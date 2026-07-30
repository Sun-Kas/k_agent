"""Thin public AG-UI ingress for the independently running Agent Backend."""

from __future__ import annotations

import json
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


class AgentAccessLayer:
    """Persist conversations and transparently relay Agent Backend AG-UI events."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        request_limiter: RequestConcurrencyLimiter,
        agent_backend_client: AgentBackendClient,
        runtime_catalog: RuntimeCatalog,
    ) -> None:
        """初始化对象依赖和内部状态。"""
        self._session_store = session_store
        self._request_limiter = request_limiter
        self._agent_backend_client = agent_backend_client
        self._runtime_catalog = runtime_catalog

    async def run(self, payload: RunAgentInput) -> StreamingResponse:
        # 前端每次运行只提交本轮新增的用户消息。历史消息由接入层从
        # SessionStore 读取并补齐，避免浏览器把旧历史重新上报造成重复或丢失。
        """说明 run 在当前模块中的具体职责。"""
        submitted = to_chat_messages(payload.messages)
        if len(submitted) != 1 or submitted[0].role != "user":
            raise HTTPException(
                status_code=400,
                detail="Each run must contain exactly one new user message",
            )
        messages = submitted
        forwarded = payload.forwarded_props or {}
        mcp_ids = self._string_list(forwarded.get("mcpServerIds"), "mcpServerIds")
        skill_ids = self._string_list(forwarded.get("skillIds"), "skillIds")
        try:
            mcp_servers, skills = self._runtime_catalog.selected_runtime(
                mcp_ids, skill_ids
            )
        except CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = await self._session_store.get_or_create(payload.thread_id)
        await self._session_store.save_run_start(
            session.id,
            messages,
            mcp_server_ids=mcp_ids,
            skill_ids=skill_ids,
        )
        context_token = update_request_context(session_id=session.id, run_id=payload.run_id)
        try:
            stream_request_context = get_request_context()
            stream_guard = self._request_limiter.protect(session.id)
            try:
                await stream_guard.__aenter__()
            except ConcurrencyLimitExceeded as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc

            async def event_generator():
                """生成对前端输出的 SSE 事件流。"""
                stream_context_token = (
                    set_request_context(stream_request_context)
                    if stream_request_context is not None
                    else update_request_context(session_id=session.id, run_id=payload.run_id)
                )
                try:
                    request_context = get_request_context()
                    request_id = request_context.request_id if request_context else "-"
                    backend_events = self._agent_backend_client.stream(
                        {
                            "threadId": session.id,
                            "runId": payload.run_id,
                            # 发送给 Agent Backend 的 messages 是完整会话历史。
                            # 接入层只负责装配传输载荷，不拼系统提示词、不压缩上下文。
                            "messages": [
                                message.model_dump(by_alias=True, mode="json")
                                for message in session.messages
                                if message.content.strip()
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
                        },
                        request_id,
                    )
                    async for event in backend_events:
                        # Agent Backend 已经输出标准 AG-UI event；这里原样持久化并
                        # 包成 SSE，不能再按自定义 thinking/tool/message 规则重排。
                        await self._session_store.append_event(session.id, event)
                        yield self._encode_sse(event)
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
        """说明 _string_list 在当前模块中的具体职责。"""
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise HTTPException(status_code=400, detail=f"{name} must be a list of strings")
        return value

    @staticmethod
    def _attachments(value: Any) -> list[dict[str, Any]]:
        """说明 _attachments 在当前模块中的具体职责。"""
        if not isinstance(value, list):
            raise HTTPException(status_code=400, detail="attachments must be a list")
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _encode_sse(event: dict[str, Any]) -> str:
        """说明 _encode_sse 在当前模块中的具体职责。"""
        return "data: " + json.dumps(
            event, ensure_ascii=False, separators=(",", ":")
        ) + "\n\n"
