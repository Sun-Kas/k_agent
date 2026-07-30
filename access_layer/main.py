"""FastAPI application for public APIs, configuration, sessions, and AG-UI ingress."""

from __future__ import annotations

from contextlib import asynccontextmanager
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
from fastapi.staticfiles import StaticFiles

from access_layer import AgentAccessLayer
from access_layer.agent_backend_client import AgentBackendClient
from access_layer.catalog import RuntimeCatalog
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
    SessionCapabilities,
    SkillsConfigUpdate,
)
from backend.config import Settings, get_or_init_settings
from backend.prompts import reset_prompt_caches
from backend.skills import SkillDefinition, clear_skill_caches, get_available_skills
from backend.skills.frontmatter import parse_bool, parse_markdown_frontmatter
from backend.storage import create_storage


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
RUNTIME_CONFIG_DIR = BACKEND_DIR / "config" / "runtime"
MODELS_CONFIG_PATH = RUNTIME_CONFIG_DIR / "models.config.json"
DATA_SKILLS_DIR = PROJECT_ROOT / "data" / "skill"
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
MAX_SKILL_ZIP_BYTES = 20 * 1024 * 1024
MAX_SKILL_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_SKILL_ZIP_FILES = 500


def _all_skills() -> list[SkillDefinition]:
    """汇总当前可编辑的 data/skill Skill 配置。"""
    return get_available_skills(Path.cwd())


def _public_skill(skill: SkillDefinition, *, editable: bool = False) -> dict:
    """Serialize a loaded skill without hiding its source boundary.

    Every editable Skill comes from data/skill. Source metadata remains visible
    so the frontend can audit that single storage boundary.
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


def _write_data_skills(skills: list[dict]) -> None:
    """Persist the complete editable Skill set under data/skill/<id>/SKILL.md."""
    DATA_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    desired_ids: set[str] = set()
    for skill in skills:
        skill_id = str(skill.get("id") or "").strip()
        normalized_id = _normalize_imported_skill_id(skill_id)
        if not skill_id or normalized_id != skill_id:
            raise HTTPException(
                status_code=400,
                detail=f'Skill ID "{skill_id}" must use lowercase letters, numbers, "-" or "_"',
            )
        if skill_id in desired_ids:
            raise HTTPException(status_code=400, detail=f'Duplicate Skill ID: "{skill_id}"')
        desired_ids.add(skill_id)

        skill_dir = (DATA_SKILLS_DIR / skill_id).resolve()
        if not _is_relative_to(skill_dir, DATA_SKILLS_DIR.resolve()):
            raise HTTPException(status_code=400, detail=f"Invalid Skill ID: {skill_id}")
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        previous_frontmatter: dict = {}
        if skill_file.exists():
            previous_frontmatter, _ = parse_markdown_frontmatter(
                skill_file.read_text(encoding="utf-8", errors="replace")
            )
        previous_frontmatter.update({
            "name": str(skill.get("name") or skill_id),
            "description": str(skill.get("description") or ""),
            "disable-model-invocation": not bool(skill.get("enabled", True)),
        })
        skill_file.write_text(
            _render_skill_markdown(previous_frontmatter, str(skill.get("instructions") or "")),
            encoding="utf-8",
        )

    for entry in DATA_SKILLS_DIR.iterdir():
        if entry.is_dir() and entry.name not in desired_ids:
            shutil.rmtree(entry)


def _render_skill_markdown(frontmatter: dict, instructions: str) -> str:
    """把配置中心的 Skill 字段渲染成 SKILL.md 内容。"""
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {_yaml_scalar(item)}" for item in value)
        elif isinstance(value, dict):
            # The lightweight frontmatter parser does not model nested YAML.
            # Preserve the value as compact JSON instead of executing it.
            lines.append(f"{key}: {_yaml_scalar(json.dumps(value, ensure_ascii=False))}")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.extend(["---", "", instructions.rstrip(), ""])
    return "\n".join(lines)


def _yaml_scalar(value: object) -> str:
    """把 Python 值转换为当前简单 frontmatter 支持的 YAML 标量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    if not text:
        return '""'
    if any(character in text for character in ":#[]{}\"'") or text != text.strip():
        return json.dumps(text, ensure_ascii=False)
    return text


def _validate_and_install_skill_zip(archive: bytes, filename: str) -> tuple[str, str, Path]:
    """Validate an uploaded Skill archive and atomically install its single root."""

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
            destination = DATA_SKILLS_DIR / skill_id
            if destination.exists():
                raise HTTPException(status_code=409, detail=f'Skill "{skill_id}" 已存在')

            with tempfile.TemporaryDirectory(prefix="k-agent-skill-") as tmp:
                staging = Path(tmp) / skill_id
                staging.mkdir(parents=True)
                # Extract entries one by one after validation. ``extractall`` is
                # intentionally avoided because archive paths are untrusted input.
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
    """Enforce file-count, entry-type, and expanded-size limits before extraction."""

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
    """Reject traversal paths, links, devices, and malformed archive metadata."""

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
    """Return the sole SKILL.md path or reject multi-Skill archives."""

    skill_files = [entry.filename for entry in entries if not entry.is_dir() and Path(entry.filename).name == "SKILL.md"]
    if not skill_files:
        raise HTTPException(status_code=400, detail="压缩包必须包含 SKILL.md")
    roots = {_skill_archive_root(path) for path in skill_files}
    if len(skill_files) > 1 or len(roots) > 1:
        raise HTTPException(status_code=400, detail="一个压缩包只能包含一个 Skill")
    return skill_files[0]


def _validate_single_skill_root(entries: list[zipfile.ZipInfo], root: tuple[str, ...]) -> None:
    """Require every imported file to live beneath the selected Skill root."""

    for entry in entries:
        if entry.is_dir():
            continue
        if _relative_skill_member(entry.filename, root) is None:
            raise HTTPException(status_code=400, detail=f"压缩包包含 Skill 目录外的文件：{entry.filename}")


def _skill_archive_root(skill_path: str) -> tuple[str, ...]:
    """根据压缩包中的 SKILL.md 路径计算 Skill 根目录。"""
    parts = Path(skill_path).parts
    return tuple(parts[:-1])


def _relative_skill_member(filename: str, root: tuple[str, ...]) -> Path | None:
    """把压缩包条目转换为相对 Skill 根目录的安全路径。"""
    parts = Path(filename).parts
    if root:
        if tuple(parts[: len(root)]) != root:
            return None
        parts = parts[len(root) :]
    if not parts:
        return None
    return Path(*parts)


def _is_ignored_zip_entry(filename: str) -> bool:
    """识别 zip 中应忽略的系统元数据文件。"""
    parts = Path(filename).parts
    return not parts or parts[0] == "__MACOSX" or any(part == ".DS_Store" for part in parts)


def _normalize_imported_skill_id(name: str) -> str:
    """把导入的 Skill 名称规范化为 data/skill 下的目录 ID。"""
    normalized = "".join(
        char.lower()
        if char.isascii() and (char.isalnum() or char == "_")
        else "-"
        for char in name
    )
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized[:80] or "skill"


def _is_relative_to(path: Path, parent: Path) -> bool:
    """判断一个路径是否位于另一个路径之下。"""
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
        "envPassthrough": server.get("envPassthrough", []),
        "cwd": server.get("cwd", ""),
        "url": server.get("url", ""),
        "bearerTokenEnv": server.get("bearerTokenEnv", ""),
        "headers": headers,
        "envHeaders": server.get("envHeaders", {}),
        "enabled": server.get("enabled", True),
    }


def _merge_mcp_runtime_status(
    servers: list[dict], runtime_statuses: list[dict]
) -> list[dict]:
    """Merge Agent Backend connection feedback into editable MCP entries."""
    statuses = {
        str(status.get("id")): status
        for status in runtime_statuses
        if status.get("id")
    }
    merged = []
    for server in servers:
        status = statuses.get(str(server.get("id")), {})
        state = str(status.get("status") or "unknown")
        merged.append(
            {
                **server,
                "connected": state == "connected",
                "status": state,
                "scope": status.get("scope", server.get("scope", "local")),
                "transport": status.get(
                    "type", server.get("transport", server.get("type", "stdio"))
                ),
                "toolCount": int(
                    status.get("tool_count", server.get("toolCount", 0)) or 0
                ),
                "resourceCount": int(
                    status.get(
                        "resource_count", server.get("resourceCount", 0)
                    )
                    or 0
                ),
                "error": status.get("error"),
            }
        )
    return merged


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
        payload = {
            key: value
            for key, value in server.items()
            if key not in {"id", "name", "description"}
        }
        serialized[server_id] = payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": serialized}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_models(settings: Settings) -> list[dict]:
    """从模型配置文件读取模型列表。"""
    if not MODELS_CONFIG_PATH.exists():
        return []
    return json.loads(MODELS_CONFIG_PATH.read_text(encoding="utf-8")).get("models", [])


def _model_api_key(model: dict, settings: Settings) -> str | None:
    """按模型配置解析真实 API key 来源。"""
    env_name = model.get("apiKeyEnv")
    if env_name:
        return os.getenv(env_name)
    return model.get("apiKey") or settings.openai_api_key


def _public_models(settings: Settings) -> list[dict]:
    """生成隐藏密钥后的前端模型配置列表。"""
    return [{
        **model,
        "apiKey": None,
        "apiKeyConfigured": bool(_model_api_key(model, settings)),
    } for model in _load_models(settings)]


def _is_deepseek_model(model: dict) -> bool:
    """判断模型配置是否属于 DeepSeek 兼容模型。"""
    marker = " ".join(str(model.get(key, "")) for key in ("id", "name", "model", "baseUrl", "base_url")).lower()
    return "deepseek" in marker


def _normalize_reasoning_effort(model: dict, effort: object) -> str | None:
    """按模型能力校验并规范化 reasoningEffort。"""
    if not model.get("supportsReasoning", False):
        return None
    value = str(effort or "none").strip().lower()
    if value == "none":
        return None
    # DeepSeek 的思考强度只接受 high/max；这里做服务端兜底，避免绕过前端传入 low/medium。
    allowed = {"high", "max"} if _is_deepseek_model(model) else {"low", "medium", "high"}
    return value if value in allowed else None


def create_app() -> FastAPI:
    """Construct the stateful public access-layer application."""

    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """管理应用启动和关闭时的资源生命周期。"""
        app.state.settings = await get_or_init_settings()
        app.state.base_system_prompt = app.state.settings.system_prompt
        app.state.storage = create_storage(app.state.settings)
        app.state.request_limiter = RequestConcurrencyLimiter(
            app.state.settings.max_concurrent_agent_requests,
            app.state.settings.request_acquire_timeout_seconds,
        )
        app.state.session_store = SessionStore(app.state.storage)
        app.state.agent_backend_client = AgentBackendClient(
            app.state.settings.agent_backend_url
        )
        app.state.runtime_catalog = RuntimeCatalog()
        app.state.runtime_catalog.ensure()
        app.state.access_layer = AgentAccessLayer(
            session_store=app.state.session_store,
            request_limiter=app.state.request_limiter,
            agent_backend_client=app.state.agent_backend_client,
            runtime_catalog=app.state.runtime_catalog,
        )
        yield

    app = FastAPI(title=settings.app_title, lifespan=lifespan)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        """为每个公开请求创建并清理请求上下文。"""
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
        """返回 Access Layer 和 Agent Backend 的健康状态。"""
        agent_backend_ok = await app.state.agent_backend_client.health()
        runtime = (
            await app.state.agent_backend_client.get_json("/internal/runtime/status")
            if agent_backend_ok
            else {}
        )
        return HealthResponse(
            ok=agent_backend_ok,
            model=app.state.settings.openai_model,
            localToolCount=int(runtime.get("localToolCount", 0)),
            mcpToolCount=int(runtime.get("mcpToolCount", 0)),
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
        """读取模型配置并隐藏敏感密钥。"""
        return {
            "path": str(MODELS_CONFIG_PATH),
            "source": str(MODELS_CONFIG_PATH),
            "models": _public_models(app.state.settings),
        }

    @app.put("/api/config/models")
    async def update_models_config(payload: ModelsConfigUpdate):
        """保存配置中心提交的模型配置。"""
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
        """由接入层读取 MCP 连接配置并合并 data 中的简要信息。"""
        path = Path(app.state.settings.mcp_config_path)
        raw, config_format = _read_local_mcp_config(path)
        summaries = {
            item["id"]: item for item in app.state.runtime_catalog.mcp_summaries()
        }
        servers = [
            {
                **_serialize_mcp_server(server),
                "name": summaries.get(str(server.get("id")), {}).get(
                    "name", server.get("id")
                ),
                "description": summaries.get(str(server.get("id")), {}).get(
                    "description", ""
                ),
                "scope": "local",
                "transport": server.get("type", "stdio"),
                "toolCount": 0,
                "resourceCount": 0,
            }
            for server in raw
        ]
        warnings = []
        runtime_statuses = []
        try:
            runtime = await app.state.agent_backend_client.get_json(
                "/internal/runtime/status"
            )
            runtime_statuses = runtime.get("mcpServers", [])
        except Exception as exc:
            warnings.append(f"Agent Backend 状态读取失败：{exc}")
        servers = _merge_mcp_runtime_status(servers, runtime_statuses)
        return {
            "path": str(path),
            "source": str(app.state.runtime_catalog.mcp_catalog_path),
            "format": config_format,
            "isTemplate": False,
            "servers": servers,
            "suppressed": [],
            "blocked": [],
            "warnings": warnings,
        }

    @app.put("/api/config/mcp")
    async def update_mcp_config(payload: McpConfigUpdate):
        """由接入层保存连接配置和 data 摘要，再通知后端重新连接。"""
        path = Path(app.state.settings.mcp_config_path)
        previous = {
            item.get("id"): item for item in _read_local_mcp_config(path)[0]
        }
        servers = []
        for profile in payload.servers:
            item = profile.model_dump(by_alias=True)
            old = previous.get(profile.id, {})
            item["env"] = {
                key: old.get("env", {}).get(key, value) if value == "***" else value
                for key, value in item.get("env", {}).items()
            }
            item["headers"] = {
                key: old.get("headers", {}).get(key, value)
                if value == "***"
                else value
                for key, value in item.get("headers", {}).items()
            }
            servers.append(item)
        _write_local_mcp_config(path, servers)
        app.state.runtime_catalog.write_mcp_summaries(servers)
        await app.state.agent_backend_client.post_json("/internal/mcp/reload")
        return {"ok": True, "restartRequired": False, **await get_mcp_config()}

    @app.get("/api/mcp/status")
    async def get_mcp_status():
        """代理读取 MCP server 连接状态。"""
        return {
            "servers": (
                await app.state.agent_backend_client.get_json(
                    "/internal/runtime/status"
                )
            ).get("mcpServers", []),
            "loadResult": None,
        }

    @app.get("/api/mcp/capabilities")
    async def get_mcp_capabilities():
        """代理读取 MCP tools/resources/prompts 能力。"""
        return await app.state.agent_backend_client.get_json(
            "/internal/mcp/capabilities"
        )

    @app.post("/api/mcp/reload")
    async def reload_mcp():
        """代理触发 Agent Backend 重新加载 MCP 连接。"""
        await app.state.agent_backend_client.post_json("/internal/mcp/reload")
        return await get_mcp_status()

    @app.get("/api/config/skills")
    async def get_skills_config():
        """由接入层读取 data 摘要，并按需附加可编辑 Skill 正文。"""
        loaded = {skill.id: skill for skill in _all_skills()}
        skills = []
        for summary in app.state.runtime_catalog.skill_summaries():
            skill = loaded.get(summary["id"])
            skills.append(
                {
                    **summary,
                    "instructions": skill.content if skill else "",
                    "source": "data",
                    "loadedFrom": "skill",
                    "filePath": skill.file_path if skill else None,
                    "baseDir": skill.base_dir if skill else None,
                    "paths": list(skill.paths) if skill else [],
                    "whenToUse": skill.when_to_use if skill else None,
                    "userInvocable": skill.user_invocable if skill else True,
                    "editable": True,
                }
            )
        return {
            "path": str(app.state.runtime_catalog.skill_catalog_path),
            "skillDir": str(DATA_SKILLS_DIR),
            "skills": skills,
            "loadedSkills": skills,
        }

    @app.put("/api/config/skills")
    async def update_skills_config(payload: SkillsConfigUpdate):
        """由接入层保存 Skill 正文和 data 摘要。"""
        skills = [skill.model_dump() for skill in payload.skills]
        _write_data_skills(skills)
        app.state.runtime_catalog.write_skill_summaries(skills)
        clear_skill_caches("access_layer_skills_updated")
        result = await get_skills_config()
        return {
            "ok": True,
            "enabledCount": sum(bool(skill.get("enabled")) for skill in skills),
            **result,
        }

    @app.get("/api/catalog")
    async def get_runtime_catalog():
        """返回工作台选择器使用的轻量 MCP/Skill 列表。"""
        return app.state.runtime_catalog.list_payload()

    @app.post("/api/skills")
    async def import_project_skill(file: UploadFile = File(...)):
        """在接入层校验并保存上传 Skill，再通知 Agent Backend 重新加载。"""
        archive = await file.read()
        skill_id, skill_name, skill_dir = _validate_and_install_skill_zip(
            archive, file.filename or "skill.zip"
        )
        clear_skill_caches("access_layer_skill_imported")
        frontmatter, _ = parse_markdown_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        )
        summaries = app.state.runtime_catalog.skill_summaries()
        summaries.append(
            {
                "id": skill_id,
                "name": skill_name,
                "description": str(frontmatter.get("description") or ""),
                "enabled": not parse_bool(
                    frontmatter.get("disable-model-invocation"), False
                ),
            }
        )
        app.state.runtime_catalog.write_skill_summaries(summaries)
        skills_payload = await get_skills_config()
        skills = skills_payload["skills"]
        return {
            "ok": True,
            "id": skill_id,
            "name": skill_name,
            "filePath": str(skill_dir / "SKILL.md"),
            "skills": skills,
            "loadedSkills": skills,
        }

    @app.post("/api/debug/prompt-cache/reset")
    async def reset_prompt_cache():
        """代理清理 Agent Backend 的 prompt 缓存。"""
        await app.state.agent_backend_client.post_json("/internal/skills/reload")
        return {"ok": True}

    @app.get("/api/debug/prompt-context")
    async def debug_prompt_context():
        """返回当前 prompt/memory/MCP 上下文调试信息。"""
        return await app.state.agent_backend_client.get_json(
            "/internal/prompt/context"
        )

    @app.post("/api/agent")
    async def run_agui_agent(payload: RunAgentInput):
        """把前端 RunAgentInput 交给接入网关处理。"""
        return await app.state.access_layer.run(payload)

    @app.get("/api/sessions")
    async def list_sessions():
        """返回持久化会话摘要列表。"""
        return await app.state.session_store.list_summaries()

    @app.get("/api/sessions/{session_id}", response_model=SessionState)
    async def get_session(session_id: str) -> SessionState:
        """返回指定会话的完整状态和 AG-UI events。"""
        session = await app.state.session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionState(
            sessionId=session.id,
            messages=session.messages,
            trace=session.trace,
            tasks=session.tasks,
            thinking=session.thinking,
            events=session.events,
            capabilities=(
                SessionCapabilities(
                    mcpServerIds=session.mcp_server_ids,
                    skillIds=session.skill_ids,
                )
                if session.mcp_server_ids is not None
                and session.skill_ids is not None
                else None
            ),
        )

    # Local built deployments expose only the Access Layer. Mounting the
    # compiled client after every API route preserves /api semantics while
    # serving the workbench and its assets from the same origin.
    if FRONTEND_DIST_DIR.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=FRONTEND_DIST_DIR, html=True),
            name="frontend",
        )

    return app
