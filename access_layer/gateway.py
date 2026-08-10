"""AG-UI 公开入口网关：承接前端单轮用户消息，装配历史后转发至 Agent Backend。

在请求链路中的角色：
- 校验前端 RunAgentInput（每轮恰好一条新 user 消息）
- 解析 MCP/Skill 选择，组装自包含运行时载荷
- 在会话锁保护下落盘本轮用户消息，再流式中继后端 AG-UI 事件
- 将事件异步持久化到 SessionStore，同时立即 SSE 推给浏览器

服务边界：
- 本层拥有会话状态与目录选择；不拼系统提示词、不执行模型/工具
- Agent Backend 为无状态执行端，只消费本层组装好的完整 messages + runtime
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ag_ui.core import RunAgentInput
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from access_layer.agent_backend_client import AgentBackendClient
from access_layer.catalog import CatalogError, RuntimeCatalog
from access_layer.concurrency import ConcurrencyLimitExceeded, RequestConcurrencyLimiter
from access_layer.request_context import (
    get_request_context,
    reset_request_context,
    set_request_context,
    update_request_context,
)
from access_layer.sessions.store import SessionStore
from backend.agui import to_chat_messages
from backend.logging_config import log_event


class AgentAccessLayer:
    """单会话 AG-UI 运行编排：持久化对话并透明中继 Agent Backend 事件流。"""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        request_limiter: RequestConcurrencyLimiter,
        agent_backend_client: AgentBackendClient,
        runtime_catalog: RuntimeCatalog,
    ) -> None:
        """注入会话存储、并发守卫、后端客户端与运行时目录。"""
        self._session_store = session_store
        self._request_limiter = request_limiter
        self._agent_backend_client = agent_backend_client
        self._runtime_catalog = runtime_catalog

    async def run(self, payload: RunAgentInput) -> StreamingResponse:
        """处理一次公开 Agent 运行：校验、加锁、落盘用户轮次并返回 SSE 流。

        前端每轮只提交新增的用户消息；完整历史由本层从 SessionStore 补齐后
        发给后端，避免浏览器重报导致重复或丢失。
        """
        submitted = to_chat_messages(payload.messages)
        if len(submitted) != 1 or submitted[0].role != "user":
            raise HTTPException(
                status_code=400,
                detail="Each run must contain exactly one new user message",
            )
        messages = submitted
        forwarded = payload.forwarded_props or {}
        agent_kind = forwarded.get("agentKind") or "k_agent"
        if not isinstance(agent_kind, str) or not agent_kind.strip():
            agent_kind = "k_agent"
        agent_kind = agent_kind.strip()
        raw_options = forwarded.get("agentOptions") or {}
        if not isinstance(raw_options, dict):
            raise HTTPException(status_code=400, detail="agentOptions must be an object")
        agent_options = dict(raw_options)
        mcp_ids = self._string_list(forwarded.get("mcpServerIds"), "mcpServerIds")
        skill_ids = self._string_list(forwarded.get("skillIds"), "skillIds")
        try:
            mcp_servers, skills = self._runtime_catalog.selected_runtime(
                mcp_ids, skill_ids
            )
        except CatalogError as exc:
            if agent_kind == "k_agent":
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # CLI runners degrade gracefully: a flaky MCP pool should not
            # block a Codex/Claude turn entirely.
            logging.getLogger("k_agent.access.gateway").warning(
                "MCP/Skill resolution failed for %s, continuing without: %s",
                agent_kind, exc,
            )
            mcp_servers = []
            skills = []
        session = await self._session_store.get_or_create(payload.thread_id)
        cli_mode = str(agent_options.get("cliSessionMode") or "ephemeral").strip().lower()
        if cli_mode not in {"ephemeral", "resume"}:
            cli_mode = "ephemeral"
        agent_options["cliSessionMode"] = cli_mode
        if (
            agent_kind != "k_agent"
            and cli_mode == "resume"
            and not agent_options.get("resumeSessionId")
        ):
            stored = session.cli_sessions.get(agent_kind)
            if stored:
                agent_options["resumeSessionId"] = stored
        context_token = update_request_context(session_id=session.id, run_id=payload.run_id)
        try:
            stream_request_context = get_request_context()
            stream_guard = self._request_limiter.protect(session.id)
            try:
                await stream_guard.__aenter__()
            except ConcurrencyLimitExceeded as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc

            # 用户轮次必须在拿到会话锁之后才持久化：若并发 run 被 429 拒绝，
            # 提前落盘会留下孤立的 user 消息。
            try:
                session = await self._session_store.save_run_start(
                    session.id,
                    messages,
                    run_id=payload.run_id,
                    mcp_server_ids=mcp_ids,
                    skill_ids=skill_ids,
                )
            except BaseException:
                await stream_guard.__aexit__(None, None, None)
                raise

            async def event_generator():
                """生成对前端输出的 SSE 事件流；落盘在后台串行，不阻塞推送。"""
                # StreamingResponse 在独立任务中消费生成器，需把请求上下文复制进该协程。
                stream_context_token = (
                    set_request_context(stream_request_context)
                    if stream_request_context is not None
                    else update_request_context(session_id=session.id, run_id=payload.run_id)
                )
                started_at = time.perf_counter()
                request_context = get_request_context()
                request_id = request_context.request_id if request_context else "-"
                try:
                    history_count = sum(
                        1 for message in session.messages if message.carries_context()
                    )
                    log_event(
                        "access.run.accepted",
                        requestId=request_id,
                        threadId=session.id,
                        runId=payload.run_id,
                        agentKind=agent_kind,
                        historyCount=history_count,
                        mcpCount=len(mcp_servers),
                        skillCount=len(skills),
                    )
                    backend_events = self._agent_backend_client.stream(
                        {
                            "threadId": session.id,
                            "runId": payload.run_id,
                            # 发给后端的是完整会话历史；本层只装配载荷，不拼系统提示。
                            "messages": [
                                message.model_dump(by_alias=True, mode="json")
                                for message in session.messages
                                if message.carries_context()
                            ],
                            "modelId": forwarded.get("modelId"),
                            # Access Layer owns selection and sends self-contained
                            # runtime entries. Agent Backend never reads list data.
                            "mcpServers": mcp_servers,
                            "skills": skills,
                            "reasoningEffort": forwarded.get("reasoningEffort"),
                            "attachments": self._attachments(
                                forwarded.get("attachments", [])
                            ),
                            "agentKind": agent_kind,
                            "agentOptions": agent_options,
                        },
                        request_id,
                    )
                    # 落盘走后台队列，SSE 帧立刻 yield，避免磁盘 I/O 卡住流式体验。
                    # 单 worker 串行写盘以保持事件顺序。
                    persist_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

                    async def _persist_worker():
                        while True:
                            event = await persist_queue.get()
                            if event is None:
                                break
                            try:
                                await self._session_store.append_event(session.id, event)
                            except Exception:
                                logging.getLogger("k_agent.access.gateway").warning(
                                    "Failed to persist event for session %s",
                                    session.id,
                                    exc_info=True,
                                )

                    persist_task = asyncio.create_task(_persist_worker())
                    try:
                        async for event in backend_events:
                            persist_queue.put_nowait(event)
                            yield self._encode_sse(event)
                    finally:
                        await persist_queue.put(None)
                        await persist_task
                    log_event(
                        "access.run.finished",
                        requestId=request_id,
                        threadId=session.id,
                        runId=payload.run_id,
                        elapsedMs=round(
                            (time.perf_counter() - started_at) * 1000,
                            3,
                        ),
                    )
                except Exception as exc:
                    log_event(
                        "access.run.failed",
                        level=logging.ERROR,
                        requestId=request_id,
                        threadId=session.id,
                        runId=payload.run_id,
                        errorType=type(exc).__name__,
                        elapsedMs=round(
                            (time.perf_counter() - started_at) * 1000,
                            3,
                        ),
                    )
                    raise
                finally:
                    await stream_guard.__aexit__(None, None, None)
                    reset_request_context(stream_context_token)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        finally:
            reset_request_context(context_token)

    @staticmethod
    def _string_list(value: Any, name: str) -> list[str]:
        """校验 forwarded_props 中的字符串 ID 列表；非法则 400。"""
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise HTTPException(status_code=400, detail=f"{name} must be a list of strings")
        return value

    @staticmethod
    def _attachments(value: Any) -> list[dict[str, Any]]:
        """过滤附件列表，只保留 dict 项后原样转发给后端。"""
        if not isinstance(value, list):
            raise HTTPException(status_code=400, detail="attachments must be a list")
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _encode_sse(event: dict[str, Any]) -> str:
        """将单个 AG-UI 事件编码为 SSE data 帧。"""
        return "data: " + json.dumps(
            event, ensure_ascii=False, separators=(",", ":")
        ) + "\n\n"
