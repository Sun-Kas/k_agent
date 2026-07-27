from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.config import get_or_init_settings
from backend.schemas import ChatMessage, SessionSummary


@dataclass(slots=True)
class SessionRecord:
    id: str
    title: str
    messages: list[ChatMessage] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    thinking: list[dict] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    async def create_session(
        self,
        title: str | None = None,
        session_id: str | None = None,
    ) -> SessionRecord:
        settings = await get_or_init_settings()
        title = title or settings.default_session_title
        session = SessionRecord(id=session_id or str(uuid.uuid4()), title=title)
        self._sessions[session.id] = session
        return session

    async def get_or_create(self, session_id: str | None) -> SessionRecord:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        return await self.create_session(session_id=session_id)

    def list_summaries(self) -> list[SessionSummary]:
        sessions = sorted(
            self._sessions.values(),
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
    ) -> SessionRecord:
        settings = await get_or_init_settings()
        session = self._sessions[session_id]
        session.messages = messages
        session.trace = trace
        session.tasks = tasks
        session.thinking = thinking or []
        session.updated_at = datetime.now(timezone.utc)
        if session.title == settings.default_session_title:
            session.title = await self._derive_title(messages)
        return session

    def get(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)

    @staticmethod
    async def _derive_title(messages: list[ChatMessage]) -> str:
        settings = await get_or_init_settings()
        for message in messages:
            if message.role == "user":
                return message.content.strip()[: settings.session_title_max_length] or settings.default_session_title
        return settings.default_session_title
