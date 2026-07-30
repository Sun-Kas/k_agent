"""Persistent conversation session store and AG-UI event projection logic."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.config import get_or_init_settings
from backend.api.schemas import ChatMessage, ChatMeta, SessionSummary
from backend.storage import StorageBackend


@dataclass(slots=True)
class SessionRecord:
    """Persisted conversation state reconstructed from accepted AG-UI events."""

    id: str
    title: str
    messages: list[ChatMessage] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    thinking: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    mcp_server_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionStore:
    """Session cache backed by the configured StorageBackend.

    This lock protects the in-memory index and persistence calls on the ASGI
    event loop. It is not the agent-run session lock; request serialization by
    session_id happens in RequestConcurrencyLimiter before execution starts.
    """

    def __init__(self, storage: StorageBackend | None = None) -> None:
        """初始化对象依赖和内部状态。"""
        self._storage = storage
        self._sessions: dict[str, SessionRecord] = {}
        self._active_run_ids: dict[str, str] = {}
        self._text_buffers: dict[tuple[str, str], dict[str, Any]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        title: str | None = None,
        session_id: str | None = None,
    ) -> SessionRecord:
        """创建新会话并写入存储后端。"""
        settings = await get_or_init_settings()
        title = title or settings.default_session_title
        session = SessionRecord(id=session_id or str(uuid.uuid4()), title=title)
        async with self._lock:
            self._sessions[session.id] = session
            await self._persist(session)
        return session

    async def get_or_create(self, session_id: str | None) -> SessionRecord:
        """按 session_id 获取会话，不存在则创建。"""
        await self._ensure_loaded()
        async with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
        return await self.create_session(session_id=session_id)

    async def list_summaries(self) -> list[SessionSummary]:
        """返回按更新时间排序的会话摘要。"""
        await self._ensure_loaded()
        async with self._lock:
            sessions = sorted(
                list(self._sessions.values()),
                key=lambda session: session.updated_at,
                reverse=True,
            )
        return [
            SessionSummary(
                id=session.id,
                title=session.title,
                updatedAt=session.updated_at,
                messageCount=len(session.messages),
            )
            for session in sessions
        ]

    async def update(
        self,
        session_id: str,
        messages: list[ChatMessage],
        trace: list[str],
        tasks: list[str],
        thinking: list[dict] | None = None,
        events: list[dict] | None = None,
    ) -> SessionRecord:
        """整体替换会话状态并持久化。"""
        settings = await get_or_init_settings()
        async with self._lock:
            session = self._sessions[session_id]
            session.messages = messages
            session.trace = trace
            session.tasks = tasks
            session.thinking = thinking or []
            session.events = events or []
            session.updated_at = datetime.now(timezone.utc)
            if session.title == settings.default_session_title:
                session.title = await self._derive_title(messages)
            await self._persist(session)
            return session

    async def save_run_start(
        self,
        session_id: str,
        messages: list[ChatMessage],
        *,
        mcp_server_ids: list[str],
        skill_ids: list[str],
    ) -> SessionRecord:
        """保存本轮用户消息并清理该会话的旧文本缓冲。"""
        settings = await get_or_init_settings()
        async with self._lock:
            session = self._sessions[session_id]
            self._active_run_ids.pop(session_id, None)
            for key in [
                key for key in self._text_buffers
                if key[0] == session_id
            ]:
                self._text_buffers.pop(key, None)
            session.messages = self._merge_messages(session.messages, messages)
            session.mcp_server_ids = list(dict.fromkeys(mcp_server_ids))
            session.skill_ids = list(dict.fromkeys(skill_ids))
            session.updated_at = datetime.now(timezone.utc)
            if session.title == settings.default_session_title:
                session.title = await self._derive_title(session.messages)
            await self._persist(session)
            return session

    async def append_event(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> SessionRecord:
        """追加 AG-UI event，并在文本结束时落盘完整 assistant 消息。"""
        async with self._lock:
            session = self._sessions[session_id]
            session.events.append(event)
            event_type = str(event.get("type") or "")

            # events 保存完整 AG-UI 流；messages 只保存可作为下一轮输入的
            # 完整对话内容。assistant 文本必须等 TEXT_MESSAGE_END 后再落盘，
            # 否则历史里会混入半截 delta 或空 assistant 占位。
            if event_type == "RUN_STARTED":
                run_id = event.get("runId")
                if isinstance(run_id, str) and run_id:
                    self._active_run_ids[session_id] = run_id
            elif event_type == "TEXT_MESSAGE_START":
                message_id = event.get("messageId")
                if isinstance(message_id, str) and message_id:
                    self._text_buffers[(session_id, message_id)] = {
                        "content": "",
                        "createdAt": datetime.now(timezone.utc),
                        "runId": self._active_run_ids.get(session_id),
                    }
            elif event_type == "TEXT_MESSAGE_CONTENT":
                message_id = event.get("messageId")
                delta = event.get("delta")
                if isinstance(message_id, str) and message_id and isinstance(delta, str):
                    buffer = self._text_buffers.setdefault(
                        (session_id, message_id),
                        {
                            "content": "",
                            "createdAt": datetime.now(timezone.utc),
                            "runId": self._active_run_ids.get(session_id),
                        },
                    )
                    buffer["content"] = str(buffer["content"]) + delta
            elif event_type == "TEXT_MESSAGE_END":
                message_id = event.get("messageId")
                if isinstance(message_id, str) and message_id:
                    buffer = self._text_buffers.pop((session_id, message_id), None)
                    if buffer is not None and str(buffer["content"]).strip():
                        message = ChatMessage(
                            id=message_id,
                            role="assistant",
                            content=str(buffer["content"]),
                            createdAt=buffer["createdAt"],
                            meta=ChatMeta(runId=buffer.get("runId")),
                        )
                        session.messages = self._upsert_message(session.messages, message)
            elif event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                self._active_run_ids.pop(session_id, None)
                for key in [
                    key for key in self._text_buffers
                    if key[0] == session_id
                ]:
                    self._text_buffers.pop(key, None)

            session.updated_at = datetime.now(timezone.utc)
            await self._persist(session)
            return session

    async def get(self, session_id: str) -> SessionRecord | None:
        """读取或创建当前对象管理的条目。"""
        await self._ensure_loaded()
        async with self._lock:
            return self._sessions.get(session_id)

    async def _ensure_loaded(self) -> None:
        """首次访问时从存储目录加载会话缓存。"""
        async with self._lock:
            if self._loaded or self._storage is None:
                self._loaded = True
                return
            settings = await get_or_init_settings()
            for path in await self._storage.list(settings.session_storage_prefix, "*.json"):
                payload = await self._storage.read_json(str(path))
                if isinstance(payload, dict):
                    session = self._record_from_payload(payload)
                    self._sessions[session.id] = session
            self._loaded = True

    async def _persist(self, session: SessionRecord) -> None:
        """把单个会话写回存储后端。"""
        if self._storage is None:
            return
        settings = await get_or_init_settings()
        await self._storage.write_json(
            f"{settings.session_storage_prefix}/{session.id}.json",
            self._record_to_payload(session),
        )

    @staticmethod
    def _record_to_payload(session: SessionRecord) -> dict[str, Any]:
        """把会话记录序列化为 JSON payload。"""
        return {
            "id": session.id,
            "title": session.title,
            "messages": [message.model_dump(mode="json", by_alias=True) for message in session.messages],
            "trace": session.trace,
            "tasks": session.tasks,
            "thinking": session.thinking,
            "events": session.events,
            "capabilities": (
                {
                    "mcpServerIds": session.mcp_server_ids,
                    "skillIds": session.skill_ids,
                }
                if session.mcp_server_ids is not None
                and session.skill_ids is not None
                else None
            ),
            "updatedAt": session.updated_at.isoformat(),
        }

    @staticmethod
    def _record_from_payload(payload: dict[str, Any]) -> SessionRecord:
        """把 JSON payload 还原为会话记录。"""
        updated_at = payload.get("updatedAt") or payload.get("updated_at")
        messages = [ChatMessage.model_validate(message) for message in payload.get("messages", [])]
        capabilities = payload.get("capabilities")
        return SessionRecord(
            id=str(payload["id"]),
            title=str(payload.get("title") or ""),
            messages=messages,
            trace=list(payload.get("trace", [])),
            tasks=list(payload.get("tasks", [])),
            thinking=list(payload.get("thinking", [])),
            events=list(payload.get("events", [])),
            mcp_server_ids=(
                list(capabilities.get("mcpServerIds", []))
                if isinstance(capabilities, dict)
                else None
            ),
            skill_ids=(
                list(capabilities.get("skillIds", []))
                if isinstance(capabilities, dict)
                else None
            ),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else datetime.now(timezone.utc),
        )

    @staticmethod
    async def _derive_title(messages: list[ChatMessage]) -> str:
        """从第一条用户消息派生会话标题。"""
        settings = await get_or_init_settings()
        for message in messages:
            if message.role == "user":
                return message.content.strip()[: settings.session_title_max_length] or settings.default_session_title
        return settings.default_session_title

    @staticmethod
    def _merge_messages(
        stored: list[ChatMessage],
        incoming: list[ChatMessage],
    ) -> list[ChatMessage]:
        """Merge client history without discarding server-completed messages."""
        merged = list(stored)
        positions = {message.id: index for index, message in enumerate(merged)}
        for message in incoming:
            index = positions.get(message.id)
            if index is None:
                positions[message.id] = len(merged)
                merged.append(message)
                continue
            existing = merged[index]
            merged[index] = existing.model_copy(
                update={
                    "role": message.role,
                    "content": message.content,
                    "meta": message.meta or existing.meta,
                }
            )
        return merged

    @staticmethod
    def _upsert_message(
        messages: list[ChatMessage],
        message: ChatMessage,
    ) -> list[ChatMessage]:
        """按消息 ID 插入或替换消息。"""
        if not any(item.id == message.id for item in messages):
            return [*messages, message]
        return [
            message if item.id == message.id else item
            for item in messages
        ]
