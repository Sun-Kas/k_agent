"""Access Layer FastAPI 应用：公开 API、配置、会话、AG-UI 入口与静态前端。

在请求链路中的角色：
- lifespan：初始化 home 布局、SessionStore、并发守卫、AgentBackendClient、
  RuntimeCatalog、TeamRuntime（后台调度）
- 中间件：为每个请求绑定 RequestContext / x-request-id
- `/api/agent` → AgentAccessLayer；Team 路由委托 TeamStore/Runtime
- 配置中心读写 `$K_AGENT_HOME`；MCP reload 等代理到后端 `/internal/*`

服务边界：本进程是唯一对外入口；Agent Backend 不暴露给浏览器。本地构建时
在全部 API 注册后再挂载 frontend/dist，保证 `/api` 语义优先。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import uuid

from ag_ui.core import RunAgentInput
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from access_layer import AgentAccessLayer
from access_layer.agent_backend_client import AgentBackendClient
from access_layer.catalog import RuntimeCatalog, catalog_fields_from_frontmatter, skill_catalog_row
from access_layer.concurrency import RequestConcurrencyLimiter, SessionAlreadyRunning
from access_layer.request_context import (
    get_request_context,
    new_request_context,
    reset_request_context,
    set_request_context,
)
from access_layer.sessions.store import SessionBusyError, SessionStore
from access_layer.sessions.workspace import (
    list_session_workspace,
    read_session_workspace_file,
    resolve_session_workspace,
)
from access_layer.scheduled_tasks import ScheduledTaskRuntime, ScheduledTaskStore
from access_layer.scheduled_tasks.router import build_scheduled_task_router
from access_layer.teams import TeamRuntime, TeamStore, build_team_router
from access_layer.schemas import (
    HealthResponse,
    McpConfigUpdate,
    ModelsConfigUpdate,
    SessionRunCancelInput,
    SessionCompactInput,
    SessionContextStatus,
    SessionSummary,
    SessionState,
    SessionCapabilities,
    SkillsConfigUpdate,
)
from access_layer.settings import Settings, get_or_init_settings
from access_layer.home import ensure_home_layout, skills_dir, teams_dir
from access_layer.logging_config import configure_agent_backend_logging
from access_layer.storage import create_storage, write_json_atomic
from access_layer.models_catalog import (
    models_path,
    load_models,
    model_api_key,
    write_models,
)
from access_layer.skills.archive import (
    is_relative_to,
    normalize_imported_skill_id,
    validate_and_install_skill_zip,
)
from access_layer.skills.frontmatter import (
    markdown_body_after_frontmatter,
    parse_bool,
    parse_markdown_frontmatter,
)
from access_layer.marketplace.router import build_marketplace_router
from access_layer.marketplace.service import MarketplaceService


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"

# Tests may patch this; production reads `$K_AGENT_HOME/content/skills`.
DATA_SKILLS_DIR: Path | None = None


def _skills_dir() -> Path:
    return DATA_SKILLS_DIR if DATA_SKILLS_DIR is not None else skills_dir()


def _write_data_skills(skills: list[dict]) -> list[dict]:
    """Persist SKILL.md packages and return catalog rows (no markdown body)."""
    root = _skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    desired_ids: set[str] = set()
    catalog_rows: list[dict] = []
    for skill in skills:
        skill_id = str(skill.get("id") or "").strip()
        normalized_id = normalize_imported_skill_id(skill_id)
        if not skill_id or normalized_id != skill_id:
            raise HTTPException(
                status_code=400,
                detail=f'Skill ID "{skill_id}" must use lowercase letters, numbers, "-" or "_"',
            )
        if skill_id in desired_ids:
            raise HTTPException(status_code=400, detail=f'Duplicate Skill ID: "{skill_id}"')
        desired_ids.add(skill_id)

        skill_dir = (root / skill_id).resolve()
        if not is_relative_to(skill_dir, root.resolve()):
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
        catalog_rows.append(
            skill_catalog_row(
                {
                    "id": skill_id,
                    "name": str(skill.get("name") or skill_id),
                    "description": str(skill.get("description") or ""),
                    "enabled": bool(skill.get("enabled", True)),
                    **catalog_fields_from_frontmatter(previous_frontmatter),
                }
            )
        )

    for entry in _skills_dir().iterdir():
        if entry.is_dir() and entry.name not in desired_ids:
            shutil.rmtree(entry)
    return catalog_rows


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
    """校验上传的 Skill zip，再原子安装到本机 skills 目录。"""

    return validate_and_install_skill_zip(archive, filename, skills_root=_skills_dir())


def _serialize_mcp_server(server: dict) -> dict:
    """保留 MCP 配置结构，但把浏览器可见的 env/headers 密钥掩码为 ***。"""
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
    write_json_atomic(path, {"mcpServers": serialized})


def _public_models(settings: Settings) -> list[dict]:
    """生成隐藏密钥后的前端模型配置列表。"""
    return [{
        **model,
        "apiKey": None,
        "apiKeyConfigured": bool(model_api_key(model, settings)),
    } for model in load_models()]


def create_app() -> FastAPI:
    """构造有状态的公开 Access Layer 应用（会话、配置、Team、AG-UI）。"""

    settings = Settings()
    if settings.server_workers > 1:
        raise RuntimeError(
            "File-backed session/context CAS requires SERVER_WORKERS=1 until a distributed lease is configured"
        )
    # 复用后端日志配置：压制第三方噪音，并过滤高频 health/catalog 访问行。
    configure_agent_backend_logging(settings.agent_backend_log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时装配依赖并启动 Team 调度；关闭时仅停止本进程拥有的 TeamRuntime。"""
        ensure_home_layout(migrate=True)
        app.state.settings = await get_or_init_settings()
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
        app.state.marketplace = MarketplaceService(app.state.runtime_catalog)
        app.state.access_layer = AgentAccessLayer(
            session_store=app.state.session_store,
            request_limiter=app.state.request_limiter,
            agent_backend_client=app.state.agent_backend_client,
            runtime_catalog=app.state.runtime_catalog,
        )
        app.state.scheduled_task_store = ScheduledTaskStore(
            teams_dir().parent / "scheduled_tasks" / "scheduled_tasks.db"
        )
        app.state.scheduled_task_runtime = ScheduledTaskRuntime(
            store=app.state.scheduled_task_store,
            access_layer=app.state.access_layer,
            session_store=app.state.session_store,
        )
        app.state.team_store = TeamStore(teams_dir() / "team_runtime.db")
        app.state.team_runtime = TeamRuntime(
            store=app.state.team_store,
            backend_client=app.state.agent_backend_client,
            runtime_catalog=app.state.runtime_catalog,
            max_active_runs=app.state.settings.team_max_active_runs,
            task_lease_seconds=app.state.settings.team_task_lease_seconds,
            enabled=app.state.settings.team_runtime_enabled,
        )
        await app.state.scheduled_task_runtime.start()
        await app.state.team_runtime.start()
        # 服务对外 ready 前先恢复已提交但未完成的 compact continuation；若后端
        # 暂不可用，checkpoint 会保留，网关也会阻止新输入越过该执行段。
        await app.state.access_layer.recover_pending_context_continuations()
        try:
            yield
        finally:
            await app.state.team_runtime.stop()
            await app.state.scheduled_task_runtime.stop()

    app = FastAPI(title=settings.app_title, lifespan=lifespan)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        """为每个公开请求创建 RequestContext，并在响应头回写 x-request-id。"""
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

    # Team 路由须在 SPA catch-all 之前注册。持久状态在 Access Layer；
    # lifespan 负责初始化 runtime，路由用代理懒解析，避免 import/构造时开库或起任务。
    class _RuntimeProxy:
        @property
        def store(self):
            return app.state.team_runtime.store

    runtime_proxy = _RuntimeProxy()
    # 路由目前只需 store；调度器留在 app.state，避免构造应用时重复启动 runtime。
    app.include_router(build_team_router(runtime_proxy))

    class _ScheduledRuntimeProxy:
        """Resolve lifespan-owned runtime lazily so app construction performs no I/O."""

        def __getattr__(self, name: str):
            return getattr(app.state.scheduled_task_runtime, name)

    app.include_router(build_scheduled_task_router(_ScheduledRuntimeProxy()))
    app.include_router(build_marketplace_router())

    @app.get("/api/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        """返回 Access Layer 和 Agent Backend 的健康状态。"""
        agent_backend_ok = await app.state.agent_backend_client.health()
        runtime: dict = {}
        backend_health: dict = {}
        if agent_backend_ok:
            runtime = await app.state.agent_backend_client.get_json(
                "/internal/runtime/status"
            )
            backend_health = await app.state.agent_backend_client.get_json(
                "/internal/health"
            )
        return HealthResponse(
            ok=agent_backend_ok,
            model=app.state.settings.openai_model,
            localToolCount=int(runtime.get("localToolCount", 0)),
            mcpToolCount=int(runtime.get("mcpToolCount", 0)),
            agentBackendOk=agent_backend_ok,
            bashSandbox=backend_health.get("bashSandbox"),
        )

    @app.get("/api/health/scheduled-tasks")
    async def scheduled_tasks_health():
        """Expose scheduler liveness without expanding the stable HealthResponse schema."""

        return await app.state.scheduled_task_runtime.health()

    @app.get("/api/config/models")
    async def get_models_config():
        """读取模型配置并隐藏敏感密钥。"""
        return {
            "path": str(models_path()),
            "source": str(models_path()),
            "models": _public_models(app.state.settings),
        }

    @app.put("/api/config/models")
    async def update_models_config(payload: ModelsConfigUpdate):
        """保存配置中心提交的模型配置。"""
        previous = {model["id"]: model for model in load_models()}
        models = []
        for profile in payload.models:
            model = profile.model_dump(by_alias=True)
            if not model.get("apiKey"):
                model["apiKey"] = previous.get(profile.id, {}).get("apiKey")
            models.append(model)
        write_models(models)
        previous_compact = {
            model_id: (item.get("compactModelId"), item.get("autoCompactEnabled", True))
            for model_id, item in previous.items()
        }
        current_compact = {
            item["id"]: (item.get("compactModelId"), item.get("autoCompactEnabled", True))
            for item in models
        }
        if previous_compact != current_compact:
            await app.state.session_store.clear_all_context_failures()
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
                "marketplace": summaries.get(str(server.get("id")), {}).get("marketplace"),
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
        return {
            "servers": (
                await app.state.agent_backend_client.get_json(
                    "/internal/runtime/status"
                )
            ).get("mcpServers", []),
            "loadResult": None,
        }

    @app.get("/api/config/skills")
    async def get_skills_config():
        """由接入层读取 catalog 摘要；配置编辑才附加 SKILL.md 正文。"""
        skills = []
        root = _skills_dir()
        for summary in app.state.runtime_catalog.skill_summaries():
            skill_dir = root / str(summary["id"])
            skill_file = skill_dir / "SKILL.md"
            instructions = ""
            if skill_file.is_file():
                instructions = markdown_body_after_frontmatter(
                    skill_file.read_text(encoding="utf-8", errors="replace")
                ).strip()
            skills.append(
                {
                    **summary,
                    "instructions": instructions,
                    "source": "home",
                    "loadedFrom": "skills",
                    "filePath": str(skill_file) if skill_file.is_file() else None,
                    "baseDir": str(skill_dir.resolve()) if skill_dir.is_dir() else None,
                    "editable": True,
                }
            )
        return {
            "path": str(app.state.runtime_catalog.skill_catalog_path),
            "skillDir": str(_skills_dir()),
            "skills": skills,
            "loadedSkills": skills,
        }

    @app.put("/api/config/skills")
    async def update_skills_config(payload: SkillsConfigUpdate):
        """由接入层保存 Skill 正文和 data 摘要。"""
        skills = [skill.model_dump() for skill in payload.skills]
        catalog_rows = _write_data_skills(skills)
        app.state.runtime_catalog.write_skill_summaries(catalog_rows)
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

    @app.get("/api/agents")
    async def list_agents():
        """代理后端：内置 + 本机探测到的 CLI agent 列表。"""

        return await app.state.agent_backend_client.get_json("/internal/agents")

    @app.post("/api/skills")
    async def import_project_skill(file: UploadFile = File(...)):
        """在接入层校验并保存上传 Skill，再通知 Agent Backend 重新加载。"""
        archive = await file.read()
        skill_id, skill_name, skill_dir = _validate_and_install_skill_zip(
            archive, file.filename or "skill.zip"
        )
        frontmatter, _ = parse_markdown_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        )
        summaries = app.state.runtime_catalog.skill_summaries()
        summaries.append(
            skill_catalog_row(
                {
                    "id": skill_id,
                    "name": skill_name,
                    "description": str(frontmatter.get("description") or ""),
                    "enabled": not parse_bool(
                        frontmatter.get("disable-model-invocation"), False
                    ),
                    **catalog_fields_from_frontmatter(frontmatter),
                }
            )
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

    @app.post("/api/agent")
    async def run_agui_agent(payload: RunAgentInput):
        """公开 AG-UI 入口：把 RunAgentInput 交给 AgentAccessLayer 编排。"""
        return await app.state.access_layer.run(payload)

    @app.get("/api/sessions")
    async def list_sessions():
        """返回持久化会话摘要列表。"""
        return await app.state.session_store.list_summaries()

    @app.get("/api/sessions/{session_id}", response_model=SessionState)
    async def get_session(session_id: str) -> SessionState:
        """返回元数据与可重放 history；Provider messages 不暴露给 UI。"""
        session = await app.state.session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        open_interrupts = await app.state.session_store.list_open_interrupts(session_id)
        return SessionState(
            sessionId=session.id,
            events=session.events,
            openInterrupts=open_interrupts,
            capabilities=(
                SessionCapabilities(
                    mcpServerIds=session.mcp_server_ids,
                    skillIds=session.skill_ids,
                    permissionMode=session.permission_mode,
                )
                if session.mcp_server_ids is not None
                and session.skill_ids is not None
                else None
            ),
        )

    @app.post("/api/sessions/{session_id}/fork", response_model=SessionSummary)
    async def fork_session(session_id: str) -> SessionSummary:
        """复制稳定的对话历史和 workspace，创建后续运行互不影响的新分支。"""

        try:
            branch = await app.state.session_store.fork_session(session_id)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if branch is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionSummary(
            id=branch.id,
            title=branch.title,
            updatedAt=branch.updated_at,
            messageCount=len(branch.messages),
        )

    @app.get(
        "/api/sessions/{session_id}/context",
        response_model=SessionContextStatus,
    )
    async def get_session_context(session_id: str) -> SessionContextStatus:
        """只公开 compact 指标；摘要正文和恢复 checkpoint 永不出 Access Layer。"""

        status = await app.state.session_store.context_status(session_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionContextStatus.model_validate(status)

    @app.post(
        "/api/sessions/{session_id}/compact",
        response_model=SessionContextStatus,
    )
    async def compact_session(
        session_id: str, payload: SessionCompactInput
    ) -> SessionContextStatus:
        """对空闲 K Agent 会话执行一次 compact-only 模型调用与 CAS 提交。"""

        try:
            async with app.state.request_limiter.protect_idle_session(session_id):
                session = await app.state.session_store.get(session_id)
                if session is None:
                    raise HTTPException(status_code=404, detail="Session not found")
                if session.open_interrupt_ids:
                    raise HTTPException(
                        status_code=409,
                        detail="Resolve or cancel the open interrupt before compacting",
                    )
                if session.agent_kind != "k_agent":
                    raise HTTPException(
                        status_code=409,
                        detail="Context compaction is available only for K Agent sessions",
                    )
                if payload.reset:
                    await app.state.session_store.reset_context(session_id)
                messages, context_state = (
                    await app.state.session_store.provider_context_state(session_id)
                )
                source_run_id = f"manual-compact-{uuid.uuid4()}"
                try:
                    result = await app.state.agent_backend_client.post_json(
                        "/internal/context/compact",
                        {
                            "threadId": session_id,
                            "sourceRunId": source_run_id,
                            "messages": [
                                message.model_dump(by_alias=True, mode="json")
                                for message in messages
                                if message.carries_context()
                            ],
                            "contextState": context_state,
                            "instructions": payload.instructions,
                            "modelId": session.model_id,
                        },
                    )
                    await app.state.session_store.commit_context_compaction(
                        session_id, dict(result.get("proposal") or {}), None
                    )
                except HTTPException:
                    raise
                except Exception as exc:
                    await app.state.session_store.record_context_failure(
                        session_id,
                        code="manual_compact_failed",
                        automatic=False,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "Context compaction failed; the complete history was preserved: "
                            f"{exc}"
                        ),
                    ) from exc
                status = await app.state.session_store.context_status(session_id)
                return SessionContextStatus.model_validate(status or {})
        except SessionAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/sessions/{session_id}/context")
    async def reset_session_context(session_id: str) -> dict[str, bool]:
        """删除可重建派生 state，不触碰完整 history。"""

        try:
            async with app.state.request_limiter.protect_idle_session(session_id):
                session = await app.state.session_store.get(session_id)
                if session is None:
                    raise HTTPException(status_code=404, detail="Session not found")
                if session.open_interrupt_ids:
                    raise HTTPException(status_code=409, detail="Open interrupt blocks context reset")
                await app.state.session_store.reset_context(session_id)
                return {"reset": True}
        except SessionAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        """删除完整会话包；运行中的会话必须先停止，避免迟到事件复活数据。"""

        try:
            deleted = await app.state.session_store.delete_session(session_id)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"deleted": True}

    @app.post("/api/sessions/{session_id}/runs/cancel")
    async def cancel_session_run(session_id: str, payload: SessionRunCancelInput):
        """取消一轮会话运行的持久化（回滚本轮 user 与半截流状态）。"""
        session = await app.state.session_store.cancel_run(session_id, payload.run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "sessionId": session.id,
            "runId": payload.run_id,
            "messageCount": len(session.messages),
        }

    @app.post("/api/sessions/{session_id}/runs/stop")
    async def stop_session_run(session_id: str, payload: SessionRunCancelInput):
        """手动结束运行并保留 user、已到达输出及 AG-UI 终止边界。"""

        session = await app.state.session_store.stop_run(session_id, payload.run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "sessionId": session.id,
            "runId": payload.run_id,
            "messageCount": len(session.messages),
            "status": "stopped",
        }

    @app.get("/api/sessions/{session_id}/workspace")
    async def get_session_workspace(session_id: str):
        """列出会话工作区中可对外预览的文件（过滤工具注入配置）。"""

        try:
            resolve_session_workspace(session_id)
            listing = list_session_workspace(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "sessionId": session_id,
            "root": listing.root,
            "files": [
                {
                    "path": item.path,
                    "name": item.name,
                    "size": item.size,
                    "modifiedAt": item.modified_at,
                }
                for item in listing.files
            ],
        }

    @app.get("/api/sessions/{session_id}/workspace/file")
    async def get_session_workspace_file(session_id: str, path: str):
        """读取会话工作区单个文件预览内容（路径逃逸失败关闭）。"""

        try:
            resolve_session_workspace(session_id)
            payload = read_session_workspace_file(session_id, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "sessionId": session_id,
            "path": payload.path,
            "name": payload.name,
            "content": payload.content,
            "truncated": payload.truncated,
            "binary": payload.binary,
            "size": payload.size,
        }

    # 本地一体部署只暴露 Access Layer。在全部 API 之后挂载编译后的前端，
    # 保证 /api 优先，同时同源提供工作台静态资源。
    if FRONTEND_DIST_DIR.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=FRONTEND_DIST_DIR, html=True),
            name="frontend",
        )

    return app
