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
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

from backend.agui import translate_agent_events
from backend.approvals import ApprovalBroker
from backend.api.schemas import ChatMessage
from backend.config import Settings, get_or_init_settings
from backend.logging_config import configure_agent_backend_logging, log_event
from backend.home import (
    ensure_home_layout,
    memory_dir,
    resolve_managed_path,
    sessions_dir,
    shared_runtime_prefix,
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
from backend.prompts import reset_prompt_caches
from backend.runners import RunnerContext, get_default_registry
from backend.runners.network_policy import network_access_enabled
from backend.runners.detect import detect_agents_payload
from backend.tools.registry import build_request_tool_set
from backend.tools.catalog import SkillCatalog
from backend.runtime_config import select_compact_model, select_model
from backend.context import generate_compaction, compose_api_messages
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
    context_summary: str = Field(default="", alias="contextSummary")
    context_state: dict[str, Any] = Field(default_factory=dict, alias="contextState")
    continuation_checkpoint: dict[str, Any] | None = Field(
        default=None, alias="continuationCheckpoint"
    )
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


class AgentBackendCompactInput(BaseModel):
    """Access Layer 发起的 compact-only 请求；浏览器不能直达该入口。"""

    thread_id: str = Field(alias="threadId")
    source_run_id: str = Field(alias="sourceRunId")
    messages: list[ChatMessage]
    context_state: dict[str, Any] = Field(default_factory=dict, alias="contextState")
    instructions: str = Field(default="", max_length=4_000)
    model_id: str | None = Field(default=None, alias="modelId")


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
        ensure_home_layout()
        await app.state.langfuse.startup()
        # 进程级连接池：本 worker 内跨 run 复用 MCP 子进程/HTTP 会话。
        app.state.mcp_pool = McpSessionPool(
            idle_ttl_seconds=settings.mcp_session_idle_ttl_seconds
        )
        # 进程级 manager 只是预热 + 运维查询用的租约持有者，不是第二套连接。
        # 每轮 run 另建轻量 manager（几份 dict）；贵的 uvx/HTTP 仍在 mcp_pool 里。
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

        try:
            return await detect_agents_payload()
        except Exception as exc:
            logging.getLogger(__name__).exception("Failed to detect agents: %s", exc)
            # Keep the UI usable even if CLI probing fails on this host.
            return {
                "defaultKind": "k_agent",
                "agents": [
                    {
                        "kind": "k_agent",
                        "name": "K Agent",
                        "available": True,
                        "command": None,
                        "version": None,
                        "detail": f"CLI detection failed: {exc}",
                        "requires_cli": False,
                        "supports_resume": False,
                        "default_cli_session_mode": "ephemeral",
                        "supportsModelSwitch": True,
                        "defaultModelId": None,
                        "models": [],
                    }
                ],
            }

    @app.get("/internal/runtime/status")
    async def runtime_status() -> dict[str, Any]:
        """本地工具数量 + 各 MCP server 连接态，供 Access Layer 展示运行时。"""
        mcp_tools = await app.state.mcp_manager.list_tools()
        tool_set = build_request_tool_set(settings=await get_or_init_settings(),
            mcp_tools=mcp_tools, mcp_manager=app.state.mcp_manager,
            skill_catalog=SkillCatalog(), authorized_servers={t.server_id for t in mcp_tools})
        return {
            "ok": True,
            "localToolCount": sum(b.spec.key.source == "local" for b in tool_set.bindings),
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

    @app.post("/internal/context/compact")
    async def compact_context(payload: AgentBackendCompactInput) -> dict[str, Any]:
        """执行禁止工具、单轮、结构化输出的手动 full compact。"""

        main_model = select_model(payload.model_id, settings)
        compact_model = select_compact_model(main_model, settings)
        client = AsyncOpenAI(
            api_key=compact_model.get("apiKey") or settings.openai_api_key,
            base_url=compact_model.get("baseUrl") or settings.openai_base_url,
        )
        provider_messages = compose_api_messages(payload.messages)
        result = await generate_compaction(
            client=client,
            model_config=compact_model,
            target_model_config=main_model,
            messages=provider_messages,
            context_state=payload.context_state,
            source_run_id=payload.source_run_id,
            trigger="manual",
            instructions=payload.instructions,
            continuation=False,
        )
        return {"proposal": result.proposal}

    @app.post("/internal/agent/run")
    async def run_agent(payload: AgentBackendRunInput, request: Request) -> StreamingResponse:
        """核心入口：按 agentKind 选 Runner，经 ApprovalBroker 合流后输出 AG-UI NDJSON。"""

        '''
        payload:#accesslayer/gateway.py/AgentAccessLayer/run/event_generator
        {
                            "threadId": session.id,
                            "runId": payload.run_id,
                            # 只发送 compact boundary 后的活动尾部；完整 history 永不离开
                            # Access Layer，也不会被压缩覆盖。
                            "messages": [
                                message.model_dump(by_alias=True, mode="json")
                                for message in provider_messages
                                if message.carries_context()
                            ],
                            "contextSummary": context_summary,
                            "contextState": context_state,
                            "continuationCheckpoint": None,
                            "modelId": forwarded.get("modelId"),
                            # Access Layer only forwards selected catalog metadata.
                            # Backend lazily reads the body after an actual Skill call;
                            # it never uses file frontmatter to rewrite this metadata.
                            "mcpServers": mcp_servers,
                            "skills": skills,
                            "reasoningEffort": forwarded.get("reasoningEffort"),
                            # Media is persisted on its owning user message. This
                            # legacy run-level field stays empty to prevent the
                            # latest turn from accidentally inheriting old media.
                            "attachments": [],
                            "agentKind": agent_kind,
                            "agentOptions": agent_options,
                            "resume": resume_entries,
                            # checkpoint 只从 SessionStore 读取并走内部边界，前端提交
                            # 的 resume payload 无法覆盖工具名、参数或 request hash。
                            "resumeCheckpoints": resume_records,
                            # Access Layer owns the session layout. Backend only
                            # receives this run's explicit working directory and
                            # must never derive a session/history path from threadId.
                            "workspaceDir": to_managed_path(
                                session_workspace_dir(session.id)
                            ),
                        }
        '''
        
        async def internal_events():
            # 每次 run 只消费 Access Layer 已解析的 MCP/Skill 元数据；Backend
            # 不读 catalog 或扫描目录，只有已授权的 Skill 工具调用会定点读取正文。
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
                # 三者独立：thread=会话，run=这次执行，request=这跳 HTTP（可多跳对同一 run）。
                thread_id=payload.thread_id,
                run_id=payload.run_id,
                request_id=request_id,
                messages=payload.messages,
                context_summary=payload.context_summary,
                context_state=payload.context_state,
                continuation_checkpoint=payload.continuation_checkpoint,
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
            # PATH/npm 走工作区 `.runtime`（指向共享 cache），不要把 cache 实路径
            # 灌进子进程；无 workspace 时才用 cache。冲突键以 agentOptions.toolEnv 为准。
            tool_env = shared_runtime_tool_env(
                shared_runtime_prefix(request_context.workspace_dir)
            )
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
                # 下一 HTTP 请求一般是新 Task，不 reset 也不会串到别人。
                # 本 Task 在 generator 返回后还会跑 ASGI 收尾/测试里同协程二次 run，
                # reset 只是把 set 前的值还回去，避免这段窗口里 current_*() 仍看到本轮策略。
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
            """把内部事件编成 NDJSON 行，交给 StreamingResponse 一块块写出。

            这里 yield 的必须是 str/bytes（一行 JSON + `\\n`），不能是 Event 对象。
            `agui_stream()` 只创建生成器；真正跑是 Starlette `async for` 拉 chunk 的时候。
            """
            # 内部 dict → AG-UI 标准事件；HTTP 边界之后前端只认这些类型。
            async for event in translate_agent_events(
                internal_events(),
                thread_id=payload.thread_id,
                run_id=payload.run_id,
            ):
                # 一行一个事件；separators 去掉空格。末尾换行才构成 NDJSON。
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

        # StreamingResponse 要的是「一块块 body」，不是 Python 对象。
        # agui_stream 必须是 async 生成器：每 yield 一行 NDJSON 字符串，Starlette
        # 就立刻 write 到 HTTP（more_body=True）。yield 事件对象本身不行。     
        return StreamingResponse(
            agui_stream(),
            media_type="application/x-ndjson",
            headers={
                # 禁止中间缓存把流攒成一整份。
                "Cache-Control": "no-cache",
                "X-Request-Id": request.headers.get("x-request-id", ""),
                # 告诉接入层/前端：body 是 AG-UI 事件，不是随意 JSON 数组。
                "X-Event-Protocol": "AG-UI",
            },
        )

    return app
