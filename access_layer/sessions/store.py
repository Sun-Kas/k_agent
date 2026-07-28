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
    id: str
    title: str
    messages: list[ChatMessage] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    thinking: list[dict] = field(default_factory=list)
    thinking_groups: list[dict] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionStore:
    """Session cache backed by the configured StorageBackend.

    This lock protects the in-memory index and persistence calls on the ASGI
    event loop. It is not the agent-run session lock; request serialization by
    session_id happens in RequestConcurrencyLimiter before execution starts.
    """

    def __init__(self, storage: StorageBackend | None = None) -> None:
        self._storage = storage
        self._sessions: dict[str, SessionRecord] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        title: str | None = None,
        session_id: str | None = None,
    ) -> SessionRecord:
        settings = await get_or_init_settings()
        title = title or settings.default_session_title
        session = SessionRecord(id=session_id or str(uuid.uuid4()), title=title)
        async with self._lock:
            self._sessions[session.id] = session
            await self._persist(session)
        return session

    async def get_or_create(self, session_id: str | None) -> SessionRecord:
        await self._ensure_loaded()
        async with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
        return await self.create_session(session_id=session_id)

    async def list_summaries(self) -> list[SessionSummary]:
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
        thinking_groups: list[dict] | None = None,
    ) -> SessionRecord:
        settings = await get_or_init_settings()
        async with self._lock:
            session = self._sessions[session_id]
            session.messages = messages
            session.trace = trace
            session.tasks = tasks
            session.thinking = thinking or []
            session.thinking_groups = thinking_groups or []
            session.updated_at = datetime.now(timezone.utc)
            if session.title == settings.default_session_title:
                session.title = await self._derive_title(messages)
            await self._persist(session)
            return session

    async def get(self, session_id: str) -> SessionRecord | None:
        await self._ensure_loaded()
        async with self._lock:
            return self._sessions.get(session_id)

    async def _ensure_loaded(self) -> None:
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
        if self._storage is None:
            return
        settings = await get_or_init_settings()
        await self._storage.write_json(
            f"{settings.session_storage_prefix}/{session.id}.json",
            self._record_to_payload(session),
        )

    @staticmethod
    def _record_to_payload(session: SessionRecord) -> dict[str, Any]:
        return {
            "id": session.id,
            "title": session.title,
            "messages": [message.model_dump(mode="json", by_alias=True) for message in session.messages],
            "trace": session.trace,
            "tasks": session.tasks,
            "thinking": session.thinking,
            "thinkingGroups": session.thinking_groups,
            "updatedAt": session.updated_at.isoformat(),
        }

    @staticmethod
    def _record_from_payload(payload: dict[str, Any]) -> SessionRecord:
        updated_at = payload.get("updatedAt") or payload.get("updated_at")
        messages = [_normalize_message_thinking_groups(ChatMessage.model_validate(message)) for message in payload.get("messages", [])]
        return SessionRecord(
            id=str(payload["id"]),
            title=str(payload.get("title") or ""),
            messages=messages,
            trace=list(payload.get("trace", [])),
            tasks=list(payload.get("tasks", [])),
            thinking=list(payload.get("thinking", [])),
            thinking_groups=_normalize_thinking_groups(list(payload.get("thinkingGroups", []))),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else datetime.now(timezone.utc),
        )

    @staticmethod
    async def _derive_title(messages: list[ChatMessage]) -> str:
        settings = await get_or_init_settings()
        for message in messages:
            if message.role == "user":
                return message.content.strip()[: settings.session_title_max_length] or settings.default_session_title
        return settings.default_session_title


def _normalize_message_thinking_groups(message: ChatMessage) -> ChatMessage:
    if message.meta is None or not message.meta.thinking_groups:
        return message
    message.meta = ChatMeta(
        toolName=message.meta.tool_name,
        thinkingGroups=_normalize_thinking_groups(message.meta.thinking_groups),
        toolActivities=message.meta.tool_activities,
        textActivities=message.meta.text_activities,
    )
    return message


def _normalize_thinking_groups(groups: list[dict]) -> list[dict]:
    """保留事件边界，并移除旧数据中误存为 thinking 的工具步骤。"""
    normalized: list[dict] = []
    for group in groups:
        if not isinstance(group, dict) or not group.get("steps"):
            continue
        steps = [
            step
            for step in group.get("steps") or []
            if isinstance(step, dict) and step.get("phase") != "tool"
        ]
        if steps:
            normalized.append({**group, "steps": steps})
    return normalized
