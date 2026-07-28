from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from ag_ui.core import RunAgentInput
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from access_layer import AgentAccessLayer
from access_layer.agent_backend_client import AgentBackendClient
from access_layer.concurrency import RequestConcurrencyLimiter
from access_layer.request_context import (
    get_request_context,
    new_request_context,
    reset_request_context,
    set_request_context,
)
from access_layer.sessions.store import SessionStore
from backend.api.schemas import (
    HealthResponse,
    McpConfigUpdate,
    ModelsConfigUpdate,
    SessionState,
    SkillsConfigUpdate,
)
from backend.config import Settings, get_or_init_settings
from backend.mcp_tool import McpClientManager, load_mcp_manager
from backend.prompts import (
    build_prompt_bundle,
    prompt_lifecycle_state,
    reset_prompt_caches,
)
from backend.skills import SkillDefinition, clear_skill_caches, get_available_skills, mcp_prompt_to_skill
from backend.skills.frontmatter import parse_markdown_frontmatter
from backend.storage import create_storage
from backend.tools import bind_request_scoped_tools, get_all_base_tools, load_local_tools
from backend.watchers import PollingChangeWatcher


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
RUNTIME_CONFIG_DIR = BACKEND_DIR / "config" / "runtime"
SKILLS_CONFIG_PATH = RUNTIME_CONFIG_DIR / "skills.config.json"
MODELS_CONFIG_PATH = RUNTIME_CONFIG_DIR / "models.config.json"
PROJECT_SKILLS_DIR = Path.cwd() / ".k_agent" / "skills"
MAX_SKILL_ZIP_BYTES = 20 * 1024 * 1024
MAX_SKILL_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_SKILL_ZIP_FILES = 500


def _load_skills() -> list[dict]:
    if not SKILLS_CONFIG_PATH.exists():
        return []
    payload = json.loads(SKILLS_CONFIG_PATH.read_text(encoding="utf-8"))
    return payload.get("skills", [])


def _configured_skills_as_definitions() -> list[SkillDefinition]:
    skills = []
    for item in _load_skills():
        name = item.get("id") or item.get("name")
        if not name:
            continue
        skills.append(
            SkillDefinition(
                id=name,
                name=name,
                description=item.get("description", ""),
                content=item.get("instructions", ""),
                source="config",
                loaded_from="config",
                disable_model_invocation=not item.get("enabled", True),
            )
        )
    return skills


def _all_skills() -> list[SkillDefinition]:
    return [*get_available_skills(Path.cwd()), *_configured_skills_as_definitions()]


def _public_skill(skill: SkillDefinition, *, editable: bool = False) -> dict:
    """Serialize a loaded skill without hiding its source boundary.

    The frontend needs to distinguish JSON-config skills, project/user skill
    folders, conditional skills, and MCP prompt-backed skills. Keeping that
    boundary visible prevents accidental edits to files the UI cannot own.
    """
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.content if editable else "",
        "enabled": not skill.disable_model_invocation,
        "source": skill.source,
        "loadedFrom": skill.loaded_from,
        "filePath": skill.file_path,
        "baseDir": skill.base_dir,
        "paths": list(skill.paths),
        "whenToUse": skill.when_to_use,
        "userInvocable": skill.user_invocable,
        "editable": editable,
    }


def _validate_and_install_skill_zip(archive: bytes, filename: str) -> tuple[str, str, Path]:
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 格式的 Skill 压缩包")
    if not archive:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(archive) > MAX_SKILL_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="压缩包超过 20MB 限制")

    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
            entries = _validated_skill_zip_entries(zip_file)
            skill_md = _find_skill_entry(entries)
            raw_skill = zip_file.read(skill_md).decode("utf-8", errors="replace")
            frontmatter, _ = parse_markdown_frontmatter(raw_skill)
            skill_name = str(frontmatter.get("name") or "").strip()
            description = str(frontmatter.get("description") or "").strip()
            if not skill_name:
                raise HTTPException(status_code=400, detail="SKILL.md frontmatter 必须包含 name")
            if not description:
                raise HTTPException(status_code=400, detail="SKILL.md frontmatter 必须包含 description")

            skill_root = _skill_archive_root(skill_md)
            _validate_single_skill_root(entries, skill_root)
            skill_id = _normalize_imported_skill_id(skill_name)
            destination = PROJECT_SKILLS_DIR / skill_id
            if destination.exists():
                raise HTTPException(status_code=409, detail=f'Skill "{skill_id}" 已存在')

            with tempfile.TemporaryDirectory(prefix="k-agent-skill-") as tmp:
                staging = Path(tmp) / skill_id
                staging.mkdir(parents=True)
                for entry in entries:
                    if entry.is_dir():
                        continue
                    relative = _relative_skill_member(entry.filename, skill_root)
                    if relative is None:
                        continue
                    target = (staging / relative).resolve()
                    if not _is_relative_to(target, staging):
                        raise HTTPException(status_code=400, detail=f"压缩包包含非法路径：{entry.filename}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zip_file.open(entry) as source, target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                if not (staging / "SKILL.md").is_file():
                    raise HTTPException(status_code=400, detail="压缩包根目录必须包含 SKILL.md")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staging), str(destination))
            return skill_id, skill_name, destination
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="压缩包无法读取，请确认文件是有效 zip") from exc


def _validated_skill_zip_entries(zip_file: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = [entry for entry in zip_file.infolist() if not _is_ignored_zip_entry(entry.filename)]
    files = [entry for entry in entries if not entry.is_dir()]
    if not files:
        raise HTTPException(status_code=400, detail="压缩包中没有可导入的文件")
    if len(files) > MAX_SKILL_ZIP_FILES:
        raise HTTPException(status_code=413, detail=f"压缩包文件数量超过 {MAX_SKILL_ZIP_FILES} 个")
    total_size = 0
    for entry in files:
        _validate_zip_member(entry)
        total_size += entry.file_size
        if total_size > MAX_SKILL_UNPACKED_BYTES:
            raise HTTPException(status_code=413, detail="压缩包解压后超过 50MB 限制")
    return entries


def _validate_zip_member(entry: zipfile.ZipInfo) -> None:
    path = Path(entry.filename)
    parts = path.parts
    if path.is_absolute() or ".." in parts:
        raise HTTPException(status_code=400, detail=f"压缩包包含非法路径：{entry.filename}")
    if entry.file_size < 0 or entry.compress_size < 0:
        raise HTTPException(status_code=400, detail=f"压缩包条目异常：{entry.filename}")
    mode = (entry.external_attr >> 16) & 0o170000
    if mode in {stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK}:
        raise HTTPException(status_code=400, detail=f"压缩包不能包含链接或特殊文件：{entry.filename}")


def _find_skill_entry(entries: list[zipfile.ZipInfo]) -> str:
    skill_files = [entry.filename for entry in entries if not entry.is_dir() and Path(entry.filename).name == "SKILL.md"]
    if not skill_files:
        raise HTTPException(status_code=400, detail="压缩包必须包含 SKILL.md")
    roots = {_skill_archive_root(path) for path in skill_files}
    if len(skill_files) > 1 or len(roots) > 1:
        raise HTTPException(status_code=400, detail="一个压缩包只能包含一个 Skill")
    return skill_files[0]


def _validate_single_skill_root(entries: list[zipfile.ZipInfo], root: tuple[str, ...]) -> None:
    for entry in entries:
        if entry.is_dir():
            continue
        if _relative_skill_member(entry.filename, root) is None:
            raise HTTPException(status_code=400, detail=f"压缩包包含 Skill 目录外的文件：{entry.filename}")


def _skill_archive_root(skill_path: str) -> tuple[str, ...]:
    parts = Path(skill_path).parts
    return tuple(parts[:-1])


def _relative_skill_member(filename: str, root: tuple[str, ...]) -> Path | None:
    parts = Path(filename).parts
    if root:
        if tuple(parts[: len(root)]) != root:
            return None
        parts = parts[len(root) :]
    if not parts:
        return None
    return Path(*parts)


def _is_ignored_zip_entry(filename: str) -> bool:
    parts = Path(filename).parts
    return not parts or parts[0] == "__MACOSX" or any(part == ".DS_Store" for part in parts)


def _normalize_imported_skill_id(name: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in name)
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized[:80] or "skill"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _serialize_mcp_server(server: dict) -> dict:
    """Preserve the MCP config format while masking browser-visible secrets."""
    env = {key: "***" for key in server.get("env", {})}
    headers = {key: "***" for key in server.get("headers", {})}
    return {
        "id": server.get("id"),
        "type": server.get("type", "stdio"),
        "command": server.get("command", ""),
        "args": server.get("args", []),
        "env": env,
        "url": server.get("url", ""),
        "headers": headers,
        "enabled": server.get("enabled", True),
    }


def _read_local_mcp_config(path: Path) -> tuple[list[dict], str | None]:
    """Read either Claude-style mcpServers or the old local servers array."""
    if not path.exists():
        return [], None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload.get("mcpServers"), dict):
        servers = [{"id": key, **value} for key, value in payload["mcpServers"].items()]
        return servers, "mcpServers"
    return payload.get("servers", []), "servers"


def _write_local_mcp_config(path: Path, servers: list[dict]) -> None:
    """Write Claude-compatible mcpServers while keeping local-only flags."""
    serialized = {}
    for server in servers:
        server_id = server["id"]
        payload = {key: value for key, value in server.items() if key != "id"}
        serialized[server_id] = payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": serialized}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _reload_runtime_integrations(app: FastAPI) -> None:
    """Hot-swap MCP/Skill state used by health and config endpoints.

    Request handling still creates per-request MCP managers because SDK streams
    are event-loop sensitive, but the app manager powers previews/status and
    should reflect config changes immediately.
    """
    previous: McpClientManager | None = getattr(app.state, "mcp_manager", None)
    next_manager = await load_mcp_manager()
    await next_manager.connect_all()
    app.state.mcp_manager = next_manager
    app.state.local_tools = await _local_tools_for_mcp(next_manager)
    reset_prompt_caches("runtime_integrations_reloaded")
    if previous is not None:
        await previous.close_all()


def _skills_with_mcp_prompts(mcp_prompts: dict[str, list[dict]]) -> list[SkillDefinition]:
    skills = _all_skills()
    for server_id, prompts in mcp_prompts.items():
        skills.extend(mcp_prompt_to_skill(server_id, prompt) for prompt in prompts)
    return skills


async def _local_tools_for_mcp(mcp_manager: McpClientManager):
    """按当前工具 preset 加载本地工具，并绑定请求级 MCP 相关闭包。"""
    return bind_request_scoped_tools(await load_local_tools(), mcp_manager)


def _load_models(settings: Settings) -> list[dict]:
    if not MODELS_CONFIG_PATH.exists():
        return []
    return json.loads(MODELS_CONFIG_PATH.read_text(encoding="utf-8")).get("models", [])


def _model_api_key(model: dict, settings: Settings) -> str | None:
    env_name = model.get("apiKeyEnv")
    if env_name:
        return os.getenv(env_name)
    return model.get("apiKey") or settings.openai_api_key


def _public_models(settings: Settings) -> list[dict]:
    return [{
        **model,
        "apiKey": None,
        "apiKeyConfigured": bool(_model_api_key(model, settings)),
    } for model in _load_models(settings)]


def _is_deepseek_model(model: dict) -> bool:
    marker = " ".join(str(model.get(key, "")) for key in ("id", "name", "model", "baseUrl", "base_url")).lower()
    return "deepseek" in marker


def _normalize_reasoning_effort(model: dict, effort: object) -> str | None:
    if not model.get("supportsReasoning", False):
        return None
    value = str(effort or "none").strip().lower()
    if value == "none":
        return None
    # DeepSeek 的思考强度只接受 high/max；这里做服务端兜底，避免绕过前端传入 low/medium。
    allowed = {"high", "max"} if _is_deepseek_model(model) else {"low", "medium", "high"}
    return value if value in allowed else None


def create_app() -> FastAPI:
    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = await get_or_init_settings()
        app.state.base_system_prompt = app.state.settings.system_prompt
        mcp_manager = await load_mcp_manager()
        await mcp_manager.connect_all()
        app.state.mcp_manager = mcp_manager
        app.state.storage = create_storage(app.state.settings)
        app.state.request_limiter = RequestConcurrencyLimiter(
            app.state.settings.max_concurrent_agent_requests,
            app.state.settings.request_acquire_timeout_seconds,
        )
        app.state.local_tools = await _local_tools_for_mcp(mcp_manager)
        app.state.session_store = SessionStore(app.state.storage)
        app.state.agent_backend_client = AgentBackendClient(
            app.state.settings.agent_backend_url
        )
        app.state.access_layer = AgentAccessLayer(
            base_system_prompt=app.state.base_system_prompt,
            session_store=app.state.session_store,
            request_limiter=app.state.request_limiter,
            agent_backend_client=app.state.agent_backend_client,
            mcp_manager_provider=lambda: app.state.mcp_manager,
            load_models=lambda: _load_models(app.state.settings),
            resolve_api_key=lambda model: _model_api_key(model, app.state.settings),
            load_skills=_all_skills,
            normalize_reasoning_effort=_normalize_reasoning_effort,
        )
        app.state.prompt_watcher = PollingChangeWatcher(
            [
                Path.cwd() / ".k_agent",
                Path.cwd() / ".claude",
                Path.cwd() / "K_AGENT.md",
                Path.cwd() / "CLAUDE.md",
                Path.home() / ".k_agent",
                SKILLS_CONFIG_PATH,
                Path(app.state.settings.mcp_config_path),
            ],
            lambda reason: (clear_skill_caches(), reset_prompt_caches(reason)),
        )
        app.state.prompt_watcher.start()
        yield
        await app.state.prompt_watcher.stop()
        await mcp_manager.close_all()

    app = FastAPI(title=settings.app_title, lifespan=lifespan)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id")
        token = set_request_context(new_request_context(str(request.url.path), request.method, request_id))
        try:
            response = await call_next(request)
            context = get_request_context()
            response.headers["x-request-id"] = context.request_id if context else request_id or ""
            return response
        finally:
            reset_request_context(token)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    @app.get("/api/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        mcp_tools = await app.state.mcp_manager.list_tools()
        agent_backend_ok = await app.state.agent_backend_client.health()
        return HealthResponse(
            ok=agent_backend_ok,
            model=app.state.settings.openai_model,
            localToolCount=len(getattr(app.state, "local_tools", get_all_base_tools())),
            mcpToolCount=len(mcp_tools),
            agentBackendOk=agent_backend_ok,
        )

    @app.get("/api/health/concurrency")
    async def concurrency_health():
        """Expose the live concurrency shape used by operations and tests."""
        snapshot = await app.state.request_limiter.snapshot()
        return {
            "serverWorkers": app.state.settings.server_workers,
            "maxConcurrentAgentRequests": snapshot.max_concurrent_requests,
            "activeRequests": snapshot.active_requests,
            "availableRequestSlots": snapshot.available_request_slots,
            "sessionLockCount": snapshot.session_lock_count,
        }

    @app.get("/api/config/models")
    async def get_models_config():
        return {
            "path": str(MODELS_CONFIG_PATH),
            "source": str(MODELS_CONFIG_PATH),
            "models": _public_models(app.state.settings),
        }

    @app.put("/api/config/models")
    async def update_models_config(payload: ModelsConfigUpdate):
        previous = {model["id"]: model for model in _load_models(app.state.settings)}
        models = []
        for profile in payload.models:
            model = profile.model_dump(by_alias=True)
            if not model.get("apiKey"):
                model["apiKey"] = previous.get(profile.id, {}).get("apiKey")
            models.append(model)
        MODELS_CONFIG_PATH.write_text(
            json.dumps({"models": models}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {"ok": True, "models": _public_models(app.state.settings)}

    @app.get("/api/config/mcp")
    async def get_mcp_config():
        settings = app.state.settings
        path = Path(settings.mcp_config_path)
        source_path = path
        is_template = False
        if not source_path.exists():
            example_path = RUNTIME_CONFIG_DIR / "mcp.config.example.json"
            if example_path.exists():
                source_path = example_path
                is_template = True
        raw_servers = []
        format_name = None
        if source_path.exists():
            raw_servers, format_name = _read_local_mcp_config(source_path)
        status_by_id = {status.id: status for status in await app.state.mcp_manager.statuses()}
        servers = []
        for server in raw_servers:
            status = status_by_id.get(server.get("id"))
            servers.append({
                **_serialize_mcp_server(server),
                "connected": status.status == "connected" if status else False,
                "status": status.status if status else "unknown",
                "scope": status.scope if status else "local",
                "transport": status.type if status else server.get("type", "stdio"),
                "toolCount": status.tool_count if status else 0,
                "resourceCount": status.resource_count if status else 0,
                "error": status.error if status else None,
            })
        return {
            "path": str(path),
            "source": str(source_path),
            "format": format_name or "mcpServers",
            "isTemplate": is_template,
            "servers": servers,
            "suppressed": app.state.mcp_manager.load_result.suppressed if app.state.mcp_manager.load_result else [],
            "blocked": app.state.mcp_manager.load_result.blocked if app.state.mcp_manager.load_result else [],
            "warnings": app.state.mcp_manager.load_result.warnings if app.state.mcp_manager.load_result else [],
        }

    @app.put("/api/config/mcp")
    async def update_mcp_config(payload: McpConfigUpdate):
        path = Path(app.state.settings.mcp_config_path)
        previous = {}
        if path.exists():
            previous_servers, _ = _read_local_mcp_config(path)
            for server in previous_servers:
                previous[server.get("id")] = server
        servers = []
        for server in payload.servers:
            item = server.model_dump(by_alias=True)
            old_env = previous.get(server.id, {}).get("env", {})
            old_headers = previous.get(server.id, {}).get("headers", {})
            item["env"] = {
                key: old_env.get(key, value) if value == "***" else value
                for key, value in item["env"].items()
            }
            item["headers"] = {
                key: old_headers.get(key, value) if value == "***" else value
                for key, value in item["headers"].items()
            }
            if item["type"] == "stdio" and not item.get("command"):
                raise HTTPException(status_code=400, detail=f'MCP server "{server.id}" requires command')
            if item["type"] in {"sse", "http", "ws"} and not item.get("url"):
                raise HTTPException(status_code=400, detail=f'MCP server "{server.id}" requires url')
            servers.append(item)
        _write_local_mcp_config(path, servers)
        await _reload_runtime_integrations(app)
        return {"ok": True, "restartRequired": False, "servers": (await get_mcp_config())["servers"]}

    @app.get("/api/mcp/status")
    async def get_mcp_status():
        return {
            "servers": [asdict(status) for status in await app.state.mcp_manager.statuses()],
            "loadResult": asdict(app.state.mcp_manager.load_result) if app.state.mcp_manager.load_result else None,
        }

    @app.get("/api/mcp/capabilities")
    async def get_mcp_capabilities():
        tools = await app.state.mcp_manager.list_tools()
        resources = await app.state.mcp_manager.list_resources()
        prompts = await app.state.mcp_manager.list_prompts()
        return {
            "tools": [asdict(tool) for tool in tools],
            "resources": resources,
            "prompts": prompts,
        }

    @app.post("/api/mcp/reload")
    async def reload_mcp():
        await _reload_runtime_integrations(app)
        return await get_mcp_status()

    @app.get("/api/config/skills")
    async def get_skills_config():
        configured = _load_skills()
        editable_ids = {item.get("id") for item in configured}
        mcp_prompts = await app.state.mcp_manager.list_prompts()
        loaded = [_public_skill(skill, editable=skill.id in editable_ids or skill.source == "config") for skill in _skills_with_mcp_prompts(mcp_prompts)]
        return {
            "path": str(SKILLS_CONFIG_PATH),
            "projectSkillDir": str((Path.cwd() / ".k_agent" / "skills").resolve()),
            "skills": configured,
            "loadedSkills": loaded,
        }

    @app.put("/api/config/skills")
    async def update_skills_config(payload: SkillsConfigUpdate):
        skills = [skill.model_dump() for skill in payload.skills]
        SKILLS_CONFIG_PATH.write_text(
            json.dumps({"skills": skills}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        clear_skill_caches()
        reset_prompt_caches("skills_config_updated")
        return {"ok": True, "enabledCount": sum(skill["enabled"] for skill in skills), "skills": (await get_skills_config())["loadedSkills"]}

    @app.post("/api/skills")
    async def import_project_skill(file: UploadFile = File(...)):
        archive = await file.read()
        skill_id, skill_name, skill_dir = _validate_and_install_skill_zip(archive, file.filename or "skill.zip")
        clear_skill_caches()
        await _reload_runtime_integrations(app)
        return {
            "ok": True,
            "id": skill_id,
            "name": skill_name,
            "baseDir": str(skill_dir.resolve()),
            "filePath": str((skill_dir / "SKILL.md").resolve()),
            "skills": (await get_skills_config())["loadedSkills"],
        }

    @app.post("/api/debug/prompt-cache/reset")
    async def reset_prompt_cache():
        state = reset_prompt_caches("api_reset")
        return {"ok": True, **state.__dict__}

    @app.get("/api/debug/prompt-context")
    async def debug_prompt_context():
        mcp_tools = await app.state.mcp_manager.list_tools()
        mcp_prompts = await app.state.mcp_manager.list_prompts()
        prompt_bundle = build_prompt_bundle(
            app.state.base_system_prompt,
            skills=_skills_with_mcp_prompts(mcp_prompts),
            mcp_tools=mcp_tools,
        )
        return {
            "lifecycle": prompt_lifecycle_state().__dict__,
            "systemPromptLength": len(prompt_bundle.system_prompt),
            "userContextKeys": list(prompt_bundle.user_context),
            "systemContextKeys": list(prompt_bundle.system_context),
            "memoryPaths": prompt_bundle.memory_paths,
            "mcpToolCount": len(mcp_tools),
            "mcpPromptCount": sum(len(items) for items in mcp_prompts.values()),
        }

    @app.post("/api/agent")
    async def run_agui_agent(payload: RunAgentInput):
        return await app.state.access_layer.run(payload)

    @app.get("/api/sessions")
    async def list_sessions():
        return await app.state.session_store.list_summaries()

    @app.get("/api/sessions/{session_id}", response_model=SessionState)
    async def get_session(session_id: str) -> SessionState:
        session = await app.state.session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionState(
            sessionId=session.id,
            messages=session.messages,
            trace=session.trace,
            tasks=session.tasks,
            thinking=session.thinking,
            thinkingGroups=session.thinking_groups,
        )

    return app
