"""Private Agent Backend: owns integrations, prompts, context and AG-UI events."""

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
from backend.home import ensure_home_layout, memory_dir, resolve_managed_path, teams_dir
from backend.mcp_tool import McpSessionPool, load_mcp_manager
from backend.sandbox import sandbox_runtime_status
from backend.observability import AgentBackendLoggingCallback, LangfuseRuntime
from backend.prompts import (
    build_prompt_bundle,
    prompt_lifecycle_state,
    reset_prompt_caches,
)
from backend.runners import RunnerContext, build_default_registry
from backend.runners.network_policy import network_access_enabled
from backend.runners.detect import detect_agents_payload
from backend.tools import load_local_tools
from backend.tools.workspace import (
    reset_tool_network_access,
    reset_tool_workspace,
    set_tool_network_access,
    set_tool_workspace,
)
from backend.watchers import PollingChangeWatcher


class AgentBackendRunInput(BaseModel):
    """Only conversation history and runtime selections cross the service boundary."""

    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    messages: list[ChatMessage]
    model_id: str | None = Field(default=None, alias="modelId")
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list, alias="mcpServers")
    skills: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_effort: str | None = Field(default=None, alias="reasoningEffort")
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    agent_kind: str | None = Field(default="k_agent", alias="agentKind")
    agent_options: dict[str, Any] = Field(default_factory=dict, alias="agentOptions")
    team_id: str | None = Field(default=None, alias="teamId")
    task_id: str | None = Field(default=None, alias="taskId")
    team_agent_id: str | None = Field(default=None, alias="teamAgentId")
    attempt_id: str | None = Field(default=None, alias="attemptId")
    workspace_dir: str | None = Field(default=None, alias="workspaceDir")


def _team_workspace(raw_path: str | None) -> Path | None:
    """Validate that a Team-provided cwd stays inside the Team state root."""

    if not raw_path:
        return None
    # Access Layer sends `$K_AGENT_HOME`-relative paths via to_managed_path().
    # Resolve against the home, not process cwd, or LAN/deploy cwd mismatches
    # reject every Team run with "workspaceDir must be inside…".
    resolved = resolve_managed_path(raw_path)
    try:
        resolved.relative_to(teams_dir().resolve())
    except ValueError as exc:
        raise ValueError("workspaceDir must be inside the Team Runtime state root") from exc
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def create_app() -> FastAPI:
    """创建 FastAPI 应用并注册服务路由。"""
    settings = Settings()
    configure_agent_backend_logging(settings.agent_backend_log_level)
    runner_registry = build_default_registry()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """管理应用启动和关闭时的资源生命周期。"""
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
        # The startup manager shares the pool with agent runs, so servers warmed
        # here are already connected when the first request selects them.
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
        """返回当前私有服务健康状态。"""
        return {
            "ok": True,
            "service": "agent-backend",
            # Agent runs carry no conversation state between requests. Integration
            # connections and caches are pooled per process, hence the qualifier.
            "stateless": "runs",
            "mcpPool": await app.state.mcp_pool.stats(),
            "langfuse": app.state.langfuse.status(),
            "bashSandbox": sandbox_runtime_status(settings),
        }

    @app.get("/internal/agents")
    async def list_agents() -> dict[str, Any]:
        """Detect built-in and local CLI agents available on this host."""

        return await detect_agents_payload()

    @app.get("/internal/runtime/status")
    async def runtime_status() -> dict[str, Any]:
        """返回本地工具和 MCP 工具的运行时统计。"""
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
        """返回 MCP tools/resources/prompts 能力清单。"""
        manager = app.state.mcp_manager
        return {
            "tools": [asdict(tool) for tool in await manager.list_tools()],
            "resources": await manager.list_resources(),
            "prompts": await manager.list_prompts(),
        }

    @app.post("/internal/mcp/reload")
    async def reload_mcp() -> dict[str, Any]:
        """代理触发 Agent Backend 重新加载 MCP 连接。"""
        log_event("mcp.reload.started")
        previous = app.state.mcp_manager
        await previous.close_all()
        # Reload is also the operator's "reconnect everything" button, so pooled
        # sessions are retired first instead of being handed to the new manager.
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
        """Drop cached prompt sections and memory.

        Skills themselves are not reloaded here: the Access Layer sends resolved
        skill definitions with each run, so there is no backend skill registry
        left to refresh.
        """

        reset_prompt_caches("agent_backend_prompt_reset")
        return {"ok": True}

    @app.get("/internal/prompt/context")
    async def prompt_context() -> dict[str, Any]:
        """返回 Agent Backend 当前 prompt 构建状态。"""
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
        """执行一次内部 Agent run 并输出 AG-UI NDJSON 流。"""
        async def internal_events():
            # 每次 run 只消费 Access Layer 已解析的 MCP/Skill 定义；
            # Agent Backend 不读取列表 JSON，也不扫描 Skill 目录。
            """生成 Agent 内部事件流。"""
            request_id = request.headers.get("x-request-id", "")
            stream_started_at = time.perf_counter()
            logging_callback = AgentBackendLoggingCallback(
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
            ctx = RunnerContext(
                thread_id=payload.thread_id,
                run_id=payload.run_id,
                request_id=request_id,
                messages=payload.messages,
                model_id=payload.model_id,
                mcp_servers=payload.mcp_servers,
                skills=payload.skills,
                reasoning_effort=payload.reasoning_effort,
                attachments=payload.attachments,
                workspace_dir=_team_workspace(payload.workspace_dir),
                options=dict(payload.agent_options or {}),
                settings=settings,
                mcp_pool=app.state.mcp_pool,
                langfuse=app.state.langfuse,
                logging_callback=logging_callback,
                approval_broker=app.state.approvals,
            )
            workspace_token = set_tool_workspace(ctx.workspace_dir)
            network_token = set_tool_network_access(network_access_enabled(ctx))
            try:
                runner = app.state.runner_registry.get(agent_kind)
                async for event in app.state.approvals.stream(
                    runner.run_stream(ctx),
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
            """把内部事件转换为 AG-UI NDJSON 流。"""
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
        """Resume a suspended Agent run after validating its routing scope."""

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

    return app
