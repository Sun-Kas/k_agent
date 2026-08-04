"""Persistent conversation session store and AG-UI event projection logic."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import get_or_init_settings
from backend.api.schemas import ChatMessage, ChatMeta, SessionSummary, ToolCallRecord
from backend.storage import StorageBackend


logger = logging.getLogger("k_agent.access_layer.sessions")


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
    # Provider-native CLI session ids (codex / claude_code) for optional resume.
    cli_sessions: dict[str, str] = field(default_factory=dict)
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
        # Tool calls are only complete once their result arrives, so the
        # START/ARGS fragments are buffered until TOOL_CALL_RESULT pairs them.
        self._tool_call_buffers: dict[tuple[str, str], dict[str, Any]] = {}
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
            await self._ensure_workspace(session.id)
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
            self._drop_session_buffers(session_id)
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
            elif event_type == "TOOL_CALL_START":
                tool_call_id = event.get("toolCallId")
                if isinstance(tool_call_id, str) and tool_call_id:
                    self._tool_call_buffers[(session_id, tool_call_id)] = {
                        "name": str(event.get("toolCallName") or "tool"),
                        "arguments": "",
                        "createdAt": datetime.now(timezone.utc),
                        "runId": self._active_run_ids.get(session_id),
                    }
            elif event_type == "TOOL_CALL_ARGS":
                tool_call_id = event.get("toolCallId")
                delta = event.get("delta")
                if isinstance(tool_call_id, str) and isinstance(delta, str):
                    buffer = self._tool_call_buffers.get((session_id, tool_call_id))
                    if buffer is not None:
                        buffer["arguments"] = str(buffer["arguments"]) + delta
            elif event_type == "TOOL_CALL_RESULT":
                session.messages = self._append_tool_turn(
                    session_id, session.messages, event
                )
            elif event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                self._active_run_ids.pop(session_id, None)
                # Tool calls still buffered here never produced a result. Dropping
                # them keeps history free of assistant tool_calls that no tool
                # message answers, which providers reject on the next run.
                self._drop_session_buffers(session_id)
            elif event_type == "CUSTOM" and event.get("name") == "cli_session":
                value = event.get("value")
                if isinstance(value, dict):
                    kind = value.get("kind")
                    provider_session_id = value.get("sessionId")
                    if isinstance(kind, str) and kind and isinstance(provider_session_id, str):
                        session.cli_sessions = {
                            **session.cli_sessions,
                            kind: provider_session_id,
                        }

            session.updated_at = datetime.now(timezone.utc)
            # Only persist on structural boundaries to avoid blocking the SSE
            # stream with disk I/O on every incremental delta/args event.
            if event_type in {
                "TEXT_MESSAGE_END",
                "TOOL_CALL_RESULT",
                "RUN_FINISHED",
                "RUN_ERROR",
                "CUSTOM",
            }:
                await self._persist(session)
            return session

    async def get(self, session_id: str) -> SessionRecord | None:
        """读取或创建当前对象管理的条目。"""
        await self._ensure_loaded()
        async with self._lock:
            return self._sessions.get(session_id)

    def _drop_session_buffers(self, session_id: str) -> None:
        """Discard partial text and tool-call state left by a finished run."""

        for buffers in (self._text_buffers, self._tool_call_buffers):
            for key in [key for key in buffers if key[0] == session_id]:
                buffers.pop(key, None)

    def _append_tool_turn(
        self,
        session_id: str,
        messages: list[ChatMessage],
        event: dict[str, Any],
    ) -> list[ChatMessage]:
        """Project a completed tool call into an assistant/tool message pair.

        The provider contract requires each tool result to follow an assistant
        message that declares the matching tool_call_id, so both halves are
        written together and only once the result is known.
        """

        tool_call_id = event.get("toolCallId")
        message_id = event.get("messageId")
        content = event.get("content")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return messages
        if not isinstance(content, str):
            return messages
        buffer = self._tool_call_buffers.pop((session_id, tool_call_id), None)
        if buffer is None:
            return messages
        created_at = buffer.get("createdAt") or datetime.now(timezone.utc)
        run_id = buffer.get("runId") or self._active_run_ids.get(session_id)
        tool_name = str(buffer.get("name") or "tool")
        call_message = ChatMessage(
            id=f"toolcall-{tool_call_id}",
            role="assistant",
            content="",
            createdAt=created_at,
            meta=ChatMeta(toolName=tool_name, runId=run_id),
            toolCalls=[
                ToolCallRecord(
                    id=tool_call_id,
                    name=tool_name,
                    arguments=str(buffer.get("arguments") or ""),
                )
            ],
        )
        result_message = ChatMessage(
            id=str(message_id) if message_id else f"toolresult-{tool_call_id}",
            role="tool",
            content=content,
            createdAt=datetime.now(timezone.utc),
            meta=ChatMeta(toolName=tool_name, runId=run_id, toolCallId=tool_call_id),
        )
        updated = self._upsert_message(messages, call_message)
        return self._upsert_message(updated, result_message)

    async def _ensure_loaded(self) -> None:
        """首次访问时从存储目录加载会话缓存。"""
        async with self._lock:
            if self._loaded or self._storage is None:
                self._loaded = True
                return
            settings = await get_or_init_settings()
            prefix = settings.session_storage_prefix
            root = self._storage.resolve(prefix)
            await self._migrate_flat_sessions(prefix, root)
            for path in await self._storage.list(prefix, "*.json"):
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                # Only accept sessions/{id}/{id}.json — never workspace package.json etc.
                if len(relative.parts) != 2 or relative.stem != relative.parts[0]:
                    continue
                try:
                    payload = await self._storage.read_json(str(path))
                    if isinstance(payload, dict):
                        session = self._record_from_payload(payload)
                        self._sessions[session.id] = session
                        await self._ensure_workspace(session.id)
                except Exception:
                    # One unreadable session file must not take down the whole
                    # session index and, with it, every other conversation.
                    logger.error("Skipping unreadable session file %s", path, exc_info=True)
            self._loaded = True

    async def _migrate_flat_sessions(self, _prefix: str, root: Path) -> None:
        """Move legacy sessions/{id}.json into sessions/{id}/{id}.json once."""

        if self._storage is None or not root.exists():
            return
        await asyncio.to_thread(self._migrate_flat_sessions_sync, root)

    @staticmethod
    def _migrate_flat_sessions_sync(root: Path) -> None:
        for path in sorted(root.glob("*.json")):
            if not path.is_file():
                continue
            session_id = path.stem
            destination = root / session_id / f"{session_id}.json"
            if destination.exists():
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(destination))
                (destination.parent / "workspace").mkdir(exist_ok=True)
                logger.info("Migrated flat session file to %s", destination)
            except OSError:
                logger.error("Failed to migrate session file %s", path, exc_info=True)

    async def _ensure_workspace(self, session_id: str) -> None:
        """Create the per-session workspace directory next to the JSON record."""

        if self._storage is None:
            return
        settings = await get_or_init_settings()
        workspace = self._storage.resolve(
            f"{settings.session_storage_prefix}/{session_id}/workspace"
        )
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)

    async def _persist(self, session: SessionRecord) -> None:
        """把单个会话写回存储后端。"""
        if self._storage is None:
            return
        settings = await get_or_init_settings()
        await self._ensure_workspace(session.id)
        await self._storage.write_json(
            f"{settings.session_storage_prefix}/{session.id}/{session.id}.json",
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
            "cliSessions": dict(session.cli_sessions),
            "updatedAt": session.updated_at.isoformat(),
        }

    @staticmethod
    def _record_from_payload(payload: dict[str, Any]) -> SessionRecord:
        """把 JSON payload 还原为会话记录。"""
        updated_at = payload.get("updatedAt") or payload.get("updated_at")
        messages = [ChatMessage.model_validate(message) for message in payload.get("messages", [])]
        capabilities = payload.get("capabilities")
        raw_cli_sessions = payload.get("cliSessions") or payload.get("cli_sessions") or {}
        cli_sessions: dict[str, str] = {}
        if isinstance(raw_cli_sessions, dict):
            for key, value in raw_cli_sessions.items():
                if isinstance(key, str) and isinstance(value, str) and key and value:
                    cli_sessions[key] = value
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
            cli_sessions=cli_sessions,
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
