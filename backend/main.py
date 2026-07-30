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

from backend.agent import AgentRunRequest, OpenAIAgent
from backend.agui import translate_agent_events
from backend.api.schemas import ChatMessage
from backend.config import Settings, get_or_init_settings
from backend.logging_config import configure_agent_backend_logging, log_event
from backend.home import ensure_home_layout, memory_dir
from backend.mcp_tool import McpSessionPool, load_mcp_manager, mcp_manager_from_runtime
from backend.sandbox import sandbox_runtime_status
from backend.observability import AgentBackendLoggingCallback, LangfuseRuntime
from backend.prompts import (
    build_prompt_bundle,
    extract_referenced_paths,
    prompt_lifecycle_state,
    reset_prompt_caches,
)
from backend.runtime_config import (
    normalize_reasoning_effort,
    select_model,
)
from backend.tools import bind_request_scoped_tools, load_local_tools
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


def create_app() -> FastAPI:
    """创建 FastAPI 应用并注册服务路由。"""
    settings = Settings()
    configure_agent_backend_logging(settings.agent_backend_log_level)

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
            log_context = {
                "requestId": request_id or "-",
                "threadId": payload.thread_id,
                "runId": payload.run_id,
            }
            stream_started_at = time.perf_counter()
            logging_callback = AgentBackendLoggingCallback(
                request_id=request_id,
                thread_id=payload.thread_id,
                run_id=payload.run_id,
            )
            log_event(
                "agent.request.received",
                requestId=request_id or "-",
                threadId=payload.thread_id,
                runId=payload.run_id,
                messageCount=len(payload.messages),
                selectedMcpServerCount=len(payload.mcp_servers),
                selectedSkillCount=len(payload.skills),
                attachmentCount=len(payload.attachments),
            )
            mcp_manager = mcp_manager_from_runtime(
                payload.mcp_servers,
                log_context=log_context,
                connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
                call_timeout_seconds=settings.mcp_call_timeout_seconds,
                session_pool=app.state.mcp_pool,
            )
            try:
                await mcp_manager.connect_all()
                model = select_model(payload.model_id, settings)
                if payload.attachments and not model.get("multimodal", False):
                    raise ValueError("Selected model does not support image input")
                skills = payload.skills
                selected_mcp_ids = {
                    str(server.get("id"))
                    for server in payload.mcp_servers
                    if server.get("id")
                }
                mcp_tools = await mcp_manager.list_tools()
                referenced_paths = extract_referenced_paths(payload.messages)
                prompt_started_at = time.perf_counter()
                log_event(
                    "prompt.compose.started",
                    **log_context,
                    basePromptChars=len(settings.system_prompt),
                    selectedSkillCount=len(skills),
                    mcpToolCount=len(mcp_tools),
                    referencedPathCount=len(referenced_paths),
                )
                prompt_bundle = build_prompt_bundle(
                    settings.system_prompt,
                    skills=skills,
                    referenced_paths=referenced_paths,
                    mcp_tools=cast(list[Any], mcp_tools),
                )
                user_context = dict(prompt_bundle.user_context)
                if payload.mcp_servers:
                    user_context["selectedMcpServers"] = "\n".join(
                        f"- {server.get('name') or server.get('id')}: "
                        f"{server.get('description') or ''}".rstrip()
                        for server in payload.mcp_servers
                    )
                # MCP 服务返回的动态指令属于本轮上下文，不写入会话历史；
                # 下一轮会根据当时连接状态重新生成。
                instructions = {
                    server_id: value
                    for server_id, value in mcp_manager.connected_instructions().items()
                    if not selected_mcp_ids or server_id in selected_mcp_ids
                }
                if instructions:
                    user_context["mcpInstructions"] = "\n\n".join(
                        f"## {server_id}\n\n{value}"
                        for server_id, value in instructions.items()
                    )
                log_event(
                    "prompt.compose.completed",
                    **log_context,
                    elapsedMs=round(
                        (time.perf_counter() - prompt_started_at) * 1000,
                        3,
                    ),
                    systemPromptChars=len(prompt_bundle.system_prompt),
                    systemContextKeys=sorted(prompt_bundle.system_context),
                    userContextKeys=sorted(user_context),
                    memoryFileCount=len(prompt_bundle.memory_paths),
                    mcpInstructionCount=len(instructions),
                    selectedSkillCount=len(skills),
                )
                tools = bind_request_scoped_tools(
                    await load_local_tools(), mcp_manager, skills
                )
                log_event(
                    "agent.context.prepared",
                    requestId=request_id or "-",
                    threadId=payload.thread_id,
                    runId=payload.run_id,
                    model=str(model.get("model") or payload.model_id or "unknown"),
                    memoryFileCount=len(prompt_bundle.memory_paths),
                    localAndSelectedToolCount=len(tools),
                    mcpToolCount=len(mcp_tools),
                )
                run_request = AgentRunRequest(
                    messages=payload.messages,
                    system_prompt=prompt_bundle.system_prompt,
                    user_context=user_context,
                    model_config=model,
                    attachments=payload.attachments,
                    mcp_server_ids=selected_mcp_ids,
                    reasoning_effort=normalize_reasoning_effort(
                        model, payload.reasoning_effort
                    ),
                    loaded_memory_paths=prompt_bundle.memory_paths,
                )
                with app.state.langfuse.observe_agent_run(
                    session_id=payload.thread_id,
                    run_id=payload.run_id,
                    model=str(model.get("model") or payload.model_id or "unknown"),
                    messages=payload.messages,
                    metadata={
                        "mcpServerIds": sorted(selected_mcp_ids),
                        "skillIds": [
                            str(skill.get("id") or skill.get("name"))
                            for skill in skills
                            if skill.get("id") or skill.get("name")
                        ],
                        "reasoningEffort": run_request.reasoning_effort,
                        "loadedMemoryPathCount": len(prompt_bundle.memory_paths),
                        "requestId": request_id,
                    },
                ) as observability_callbacks:
                    agent = OpenAIAgent(
                        tools,
                        mcp_manager,
                        callbacks=[logging_callback, *observability_callbacks],
                        skills=skills,
                    )
                    async for event in agent.run_stream(run_request):
                        yield event
            except asyncio.CancelledError:
                log_event(
                    "agent.stream.cancelled",
                    requestId=request_id or "-",
                    threadId=payload.thread_id,
                    runId=payload.run_id,
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
                    elapsedMs=round(
                        (time.perf_counter() - stream_started_at) * 1000,
                        3,
                    ),
                    errorType=type(exc).__name__,
                )
                raise
            finally:
                await mcp_manager.close_all()
                log_event(
                    "agent.stream.closed",
                    requestId=request_id or "-",
                    threadId=payload.thread_id,
                    runId=payload.run_id,
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

    return app
