"""私有 Agent Backend HTTP 服务：无会话状态，只负责单次 run 的模型/工具执行与 AG-UI 流。

Access Layer 持有会话与并发；本进程按请求组装 Runner、MCP、workspace/env，
把内部事件翻译成 AG-UI NDJSON 后返回。进程级只缓存 MCP 连接池、审批经纪与
prompt 失效监听，不在请求间保留对话内容。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.agui import translate_agent_events
from backend.approvals import ApprovalBroker
from backend.api.schemas import ApprovalResolutionInput, ChatMessage
from backend.config import Settings, get_or_init_settings
from backend.logging_config import configure_agent_backend_logging, log_event
from backend.home import (
    ensure_home_layout,
    ensure_shared_runtime,
    link_shared_runtime,
    memory_dir,
    resolve_managed_path,
    sessions_dir,
    shared_runtime_tool_env,
    teams_dir,
)
from backend.mcp_tool import McpSessionPool, load_mcp_manager
from backend.sandbox import (
    reset_tool_env_overrides,
    sandbox_runtime_status,
    set_tool_env_overrides,
)
from backend.observability import AgentBackendLoggingObserver, LangfuseRuntime
from backend.prompts import (
    build_prompt_bundle,
    prompt_lifecycle_state,
    reset_prompt_caches,
)
from backend.runners import RunnerContext, get_default_registry
from backend.runners.network_policy import network_access_enabled
from backend.runners.detect import detect_agents_payload
from backend.tools import load_local_tools
from backend.tools.workspace import (
    reset_tool_permission_mode,
    reset_tool_network_access,
    reset_tool_workspace,
    set_tool_permission_mode,
    set_tool_network_access,
    set_tool_workspace,
)
from backend.watchers import PollingChangeWatcher


class AgentBackendRunInput(BaseModel):
    """跨服务边界的唯一入参：对话历史 + 本轮选中的模型/MCP/Skill/工作区等。"""

    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    messages: list[ChatMessage]
    model_id: str | None = Field(default=None, alias="modelId")
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list, alias="mcpServers")
    skills: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_effort: str | None = Field(default=None, alias="reasoningEffort")
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    resume: list[dict[str, Any]] = Field(default_factory=list)
    resume_checkpoints: list[dict[str, Any]] = Field(
        default_factory=list, alias="resumeCheckpoints"
    )
    agent_kind: str | None = Field(default="k_agent", alias="agentKind")
    agent_options: dict[str, Any] = Field(default_factory=dict, alias="agentOptions")
    team_id: str | None = Field(default=None, alias="teamId")
    task_id: str | None = Field(default=None, alias="taskId")
    team_agent_id: str | None = Field(default=None, alias="teamAgentId")
    attempt_id: str | None = Field(default=None, alias="attemptId")
    workspace_dir: str = Field(alias="workspaceDir", min_length=1)


def _resolve_run_workspace(raw_path: str, *, is_team_run: bool) -> Path:
    """只接受 Access Layer 下发的工作区，不用 threadId 推导任何会话路径。"""

    # Access Layer sends `$K_AGENT_HOME`-relative paths via to_managed_path().
    # Resolve against the home, not process cwd, or LAN/deploy cwd mismatches
    # reject valid run workspaces after the two services start from different cwd.
    resolved = resolve_managed_path(raw_path)
    allowed_root = teams_dir().resolve() if is_team_run else sessions_dir().resolve()
    try:
        relative = resolved.relative_to(allowed_root)
    except ValueError as exc:
        scope = "Team Runtime" if is_team_run else "session"
        raise ValueError(f"workspaceDir must be inside the {scope} workspace root") from exc
    if not is_team_run and (len(relative.parts) != 2 or relative.parts[-1] != "workspace"):
        # A normal run may access only sessions/{id}/workspace, never the sibling
        # sessions/{id}/{id}.json conversation record owned by Access Layer.
        raise ValueError("workspaceDir must identify a session workspace")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def create_app() -> FastAPI:
    """组装 Agent Backend：内部健康/能力探测 + `/internal/agent/run` AG-UI 流。"""
    settings = Settings()
    configure_agent_backend_logging(settings.agent_backend_log_level)
    runner_registry = get_default_registry()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时预热 home/MCP/Langfuse；关闭时停 watcher 并强制回收连接池。"""
        log_event(
            "service.starting",
            host=settings.agent_backend_host,
            port=settings.agent_backend_port,
            workers=settings.server_workers,
        )
        await get_or_init_settings()
        app.state.langfuse = LangfuseRuntime(settings)
        ensure_home_layout(migrate=True)
        await app.state.langfuse.startup()
        app.state.mcp_pool = McpSessionPool(
            idle_ttl_seconds=settings.mcp_session_idle_ttl_seconds
        )
        # 启动时的 manager 与 agent run 共用连接池，预热过的 server
        # 在首个请求选中时往往已经连上。
        manager = await load_mcp_manager(app.state.mcp_pool)
        await manager.connect_all()
        app.state.mcp_manager = manager
        app.state.runner_registry = runner_registry
        app.state.approvals = ApprovalBroker()
        app.state.runtime_watcher = PollingChangeWatcher(
            [
                Path.cwd() / "CLAUDE.md",
                Path.cwd() / ".claude" / "rules",
                memory_dir(),
                Path(settings.mcp_config_path),
            ],
            reset_prompt_caches,
        )
        app.state.runtime_watcher.start()
        statuses = await manager.statuses()
        log_event(
            "service.ready",
            mcpServerCount=len(statuses),
            connectedMcpServerCount=sum(
                status.status == "connected" for status in statuses
            ),
            failedMcpServerCount=sum(status.status == "failed" for status in statuses),
            langfuseEnabled=app.state.langfuse.enabled,
            agentKinds=runner_registry.kinds(),
        )
        try:
            yield
        finally:
            log_event("service.stopping")
            await app.state.runtime_watcher.stop()
            await manager.close_all()
            await app.state.mcp_pool.close_all(force=True)
            await app.state.langfuse.shutdown()
            log_event("service.stopped")

    app = FastAPI(title=f"{settings.app_title} - Agent Backend", lifespan=lifespan)

    @app.get("/internal/health")
    async def health() -> dict[str, Any]:
        """进程存活探针；顺带暴露 MCP 池占用与 bash sandbox 能力。"""
        return {
            "ok": True,
            "service": "agent-backend",
            # Agent run 在请求间不携带对话状态。集成连接与缓存按进程池化，故有此限定。
            "stateless": "runs",
            "mcpPool": await app.state.mcp_pool.stats(),
            "langfuse": app.state.langfuse.status(),
            "bashSandbox": sandbox_runtime_status(settings),
        }

    @app.get("/internal/agents")
    async def list_agents() -> dict[str, Any]:
        """探测本机可用的内置/CLI Agent（k_agent、codex、claude_code 等）。"""

        return await detect_agents_payload()

    @app.get("/internal/runtime/status")
    async def runtime_status() -> dict[str, Any]:
        """本地工具数量 + 各 MCP server 连接态，供 Access Layer 展示运行时。"""
        local_tools = await load_local_tools()
        mcp_tools = await app.state.mcp_manager.list_tools()
        return {
            "ok": True,
            "localToolCount": len(local_tools),
            "mcpToolCount": len(mcp_tools),
            "mcpServers": [
                asdict(status) for status in await app.state.mcp_manager.statuses()
            ],
        }

    @app.get("/internal/mcp/capabilities")
    async def mcp_capabilities() -> dict[str, Any]:
        """枚举已连接 MCP 的 tools/resources/prompts，供前端能力面板使用。"""
        manager = app.state.mcp_manager
        return {
            "tools": [asdict(tool) for tool in await manager.list_tools()],
            "resources": await manager.list_resources(),
            "prompts": await manager.list_prompts(),
        }

    @app.post("/internal/mcp/reload")
    async def reload_mcp() -> dict[str, Any]:
        """运维入口：关闭旧 manager+池内会话后按最新配置全量重连。"""
        log_event("mcp.reload.started")
        previous = app.state.mcp_manager
        await previous.close_all()
        # Reload 也是运维的「全部重连」按钮：先退役池内会话，
        # 再交给新 manager，而不是把旧连接直接移交。
        await app.state.mcp_pool.close_all()
        manager = await load_mcp_manager(app.state.mcp_pool)
        await manager.connect_all()
        app.state.mcp_manager = manager
        status = await runtime_status()
        log_event(
            "mcp.reload.completed",
            mcpServerCount=len(status["mcpServers"]),
            mcpToolCount=status["mcpToolCount"],
        )
        return status

    @app.post("/internal/prompt/reset")
    async def reset_prompt_cache() -> dict[str, Any]:
        """丢弃进程内 prompt section / memory 缓存。

        Skill 不在此重载：Access Layer 每轮随请求下发已解析定义，
        Backend 没有独立的 Skill 注册表可刷新。
        """

        reset_prompt_caches("agent_backend_prompt_reset")
        return {"ok": True}

    @app.get("/internal/prompt/context")
    async def prompt_context() -> dict[str, Any]:
        """调试用：当前 prompt 生命周期代数与拼装后的上下文键。"""
        manager = app.state.mcp_manager
        mcp_tools = await manager.list_tools()
        mcp_prompts = await manager.list_prompts()
        prompt_bundle = build_prompt_bundle(
            settings.system_prompt,
            skills=[],
            mcp_tools=cast(list[Any], mcp_tools),
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

    @app.post("/internal/agent/run")
    async def run_agent(payload: AgentBackendRunInput, request: Request) -> StreamingResponse:
        """核心入口：按 agentKind 选 Runner，经 ApprovalBroker 合流后输出 AG-UI NDJSON。"""
        async def internal_events():
            # 每次 run 只消费 Access Layer 已解析的 MCP/Skill 定义；
            # Agent Backend 不读取列表 JSON，也不扫描 Skill 目录。
            """绑定 workspace/网络/共享 runtime env，驱动 Runner 并产出内部事件。"""
            request_id = request.headers.get("x-request-id", "")
            stream_started_at = time.perf_counter()
            logging_observer = AgentBackendLoggingObserver(
                request_id=request_id,
                thread_id=payload.thread_id,
                run_id=payload.run_id,
            )
            agent_kind = (payload.agent_kind or "k_agent").strip() or "k_agent"
            log_event(
                "agent.request.received",
                requestId=request_id or "-",
                threadId=payload.thread_id,
                runId=payload.run_id,
                agentKind=agent_kind,
                messageCount=len(payload.messages),
                selectedMcpServerCount=len(payload.mcp_servers),
                selectedSkillCount=len(payload.skills),
                attachmentCount=len(payload.attachments),
            )
            request_context = RunnerContext(
                thread_id=payload.thread_id,
                run_id=payload.run_id,
                request_id=request_id,
                messages=payload.messages,
                model_id=payload.model_id,
                mcp_servers=payload.mcp_servers,
                skills=payload.skills,
                reasoning_effort=payload.reasoning_effort,
                attachments=payload.attachments,
                resume=payload.resume,
                resume_checkpoints=payload.resume_checkpoints,
                workspace_dir=_resolve_run_workspace(
                    payload.workspace_dir,
                    is_team_run=bool(payload.team_id),
                ),
                team_id=payload.team_id,
                options=dict(payload.agent_options or {}),
                settings=settings,
                mcp_pool=app.state.mcp_pool,
                langfuse=app.state.langfuse,
                logging_observer=logging_observer,
                approval_broker=app.state.approvals,
            )
            # worksapce地址
            workspace_token = set_tool_workspace(request_context.workspace_dir)
            # 网络访问权限
            network_token = set_tool_network_access(network_access_enabled(request_context))
            # 权限模式
            permission_token = set_tool_permission_mode(
                str(request_context.options.get("permissionMode") or "default")
            )
            # 共享 runtime env，全项目一份 Node/npm 前缀，对话与 Team 共用。冲突键以
            # agentOptions.toolEnv 为准。
            shared_runtime = ensure_shared_runtime()
            if request_context.workspace_dir is not None:
                link_shared_runtime(request_context.workspace_dir, shared_runtime)
            tool_env = shared_runtime_tool_env(shared_runtime)
            option_env = (
                request_context.options.get("toolEnv")
                if isinstance(request_context.options, dict)
                else None
            )
            if isinstance(option_env, dict):
                tool_env.update(
                    {str(k): str(v) for k, v in option_env.items() if v is not None}
                )
            env_token = set_tool_env_overrides(tool_env)
            try:
                runner = app.state.runner_registry.get(agent_kind)
                # 每轮 run 都要挂审批合流：无 HITL 时只是原样转发 Runner 事件；
                # 有 HITL 时 request() 才能把卡片插入这条正在输出的 HTTP 流。
                async for event in app.state.approvals.stream(
                    runner.run_stream(request_context),
                    thread_id=payload.thread_id,
                    run_id=payload.run_id,
                ):
                    yield event
            except asyncio.CancelledError:
                log_event(
                    "agent.stream.cancelled",
                    requestId=request_id or "-",
                    threadId=payload.thread_id,
                    runId=payload.run_id,
                    agentKind=agent_kind,
                    elapsedMs=round(
                        (time.perf_counter() - stream_started_at) * 1000,
                        3,
                    ),
                )
                raise
            except Exception as exc:
                log_event(
                    "agent.stream.failed",
                    level=logging.ERROR,
                    requestId=request_id or "-",
                    threadId=payload.thread_id,
                    runId=payload.run_id,
                    agentKind=agent_kind,
                    elapsedMs=round(
                        (time.perf_counter() - stream_started_at) * 1000,
                        3,
                    ),
                    errorType=type(exc).__name__,
                )
                raise
            finally:
                reset_tool_env_overrides(env_token)
                reset_tool_permission_mode(permission_token)
                reset_tool_network_access(network_token)
                reset_tool_workspace(workspace_token)
                log_event(
                    "agent.stream.closed",
                    requestId=request_id or "-",
                    threadId=payload.thread_id,
                    runId=payload.run_id,
                    agentKind=agent_kind,
                    elapsedMs=round(
                        (time.perf_counter() - stream_started_at) * 1000,
                        3,
                    ),
                )

        async def agui_stream():
            # Agent 内部事件在这里统一转换为 AG-UI。HTTP 边界之后只允许
            # 标准 start/content/end/result 事件，前端按到达顺序渲染即可。
            """`translate_agent_events` → 逐行 JSON，媒体类型 application/x-ndjson。"""
            async for event in translate_agent_events(
                internal_events(),
                thread_id=payload.thread_id,
                run_id=payload.run_id,
            ):
                yield json.dumps(
                    jsonable_encoder(
                        event.model_dump(
                            by_alias=True,
                            mode="json",
                            exclude_none=True,
                        )
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\n"

        return StreamingResponse(
            agui_stream(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Request-Id": request.headers.get("x-request-id", ""),
                "X-Event-Protocol": "AG-UI",
            },
        )

    @app.post("/internal/approvals/{request_id}")
    async def resolve_approval(
        request_id: str, payload: ApprovalResolutionInput
    ) -> dict[str, Any]:
        """人类审批回调：校验 threadId/runId 后唤醒挂起的工具调用。"""

        resolved = await app.state.approvals.resolve(
            request_id,
            thread_id=payload.thread_id,
            run_id=payload.run_id,
            decision=payload.model_dump(by_alias=True),
        )
        if not resolved:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Approval request is no longer pending")
        return {"ok": True, "requestId": request_id}

    @app.get("/internal/approvals/{request_id}")
    async def get_approval_status(
        request_id: str, threadId: str, runId: str
    ) -> dict[str, Any]:
        """Expose only whether this exact run-scoped approval is still actionable."""

        pending = await app.state.approvals.is_pending(
            request_id, thread_id=threadId, run_id=runId
        )
        return {"ok": True, "requestId": request_id, "pending": pending}

    return app
