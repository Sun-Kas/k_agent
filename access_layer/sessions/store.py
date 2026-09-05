"""会话持久化与 AG-UI 事件投影：Access Layer 拥有对话真相源。

在请求链路中的角色：
- `save_run_start`：在会话锁保护下写入本轮 user 消息
- `append_event`：消费后端 AG-UI 流，投影为可再输入的 messages，并按边界落盘
- `cancel_run`：撤销本轮用户消息与半截流状态
- `stop_run`：保留本轮已接收内容并写入稳定的手动终止边界

服务边界：
- 存储布局 `sessions/{id}/session.json + history.jsonl + context/`；本层拥有，后端无状态
- 本类的 asyncio.Lock 只保护内存索引与落盘，不是 agent-run 互斥
  （互斥在 RequestConcurrencyLimiter）
- SSE 仍转发 token delta；events 只在结构边界写入累计后的 CONTENT/ARGS
- messages 同样只在 TEXT_MESSAGE_END / TOOL_CALL_RESULT 等边界写入完整内容
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from access_layer.sessions.durable_events import (
    INCREMENTAL_EVENT_TYPES,
    coalesce_durable_events,
)
from access_layer.sessions.history import (
    events_from_records,
    is_durable_event,
    make_record,
    message_seq_index,
    messages_from_records,
    projected_prefix_digest,
)
from access_layer.sessions.migrate_history import migrate_all_sessions, migrate_session_record
from access_layer.sessions.context_store import ContextStateStore, empty_context_state
from access_layer.settings import get_or_init_settings
from access_layer.schemas import ChatMessage, ChatMeta, SessionSummary, ToolCallRecord
from access_layer.storage import StorageBackend
from access_layer.user_questions import normalize_user_question_answers


logger = logging.getLogger("k_agent.access_layer.sessions")


class SessionBusyError(RuntimeError):
    """Raised when a destructive session operation races an active run."""


class OpenInterruptError(RuntimeError):
    """线程存在未解决 Interrupt，普通用户输入不能越过该恢复边界。"""


class ResumeConflictError(RuntimeError):
    """Resume 未完整覆盖开放 Interrupt，或与已经认领的决定冲突。"""


@dataclass(slots=True)
class SessionRecord:
    """会话元数据及由 history 临时投影的 messages/events。"""

    id: str
    title: str
    messages: list[ChatMessage] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    history_records: list[dict[str, Any]] = field(default_factory=list)
    next_history_seq: int = 1
    mcp_server_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    permission_mode: str = "default"
    # 手动 compact 默认沿用该会话最近一次 K Agent 主模型，而不是全局默认模型。
    model_id: str | None = None
    agent_kind: str = "k_agent"
    # Provider-native CLI session ids (codex / claude_code) for optional resume.
    cli_sessions: dict[str, str] = field(default_factory=dict)
    # 主会话文件只保存开放 ID 索引；完整审批与 checkpoint 独立原子落盘。
    open_interrupt_ids: list[str] = field(default_factory=list)
    # 来源是持久化边界：自动任务仍有完整 session/workspace，但不进入普通会话目录。
    source: str = "interactive"
    source_ref: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionStore:
    """基于 StorageBackend 的会话缓存与 AG-UI→messages 投影器。

    `_lock` 保护 ASGI 事件循环上的内存索引与持久化调用。它不是 agent-run
    会话锁；按 session_id 的请求串行化在 RequestConcurrencyLimiter 中完成。
    """

    def __init__(self, storage: StorageBackend | None = None) -> None:
        """绑定存储后端，并初始化内存索引、缓冲与取消标记表。"""
        self._storage = storage
        self._context_store = ContextStateStore(storage)
        self._sessions: dict[str, SessionRecord] = {}
        # 无 StorageBackend 的单元测试也需要同一状态机；生产环境同时写独立文件。
        self._approval_records: dict[tuple[str, str], dict[str, Any]] = {}
        self._resume_intents: dict[tuple[str, str], dict[str, Any]] = {}
        self._active_run_ids: dict[str, str] = {}
        self._run_input_message_ids: dict[tuple[str, str], set[str]] = {}
        self._cancelled_run_ids: set[tuple[str, str]] = set()
        self._cancelled_active_run_ids: dict[str, str] = {}
        # 手动停止与撤销不同：已接收内容保留，但终止点之后的迟到事件必须丢弃。
        self._stopped_run_ids: set[tuple[str, str]] = set()
        self._stopped_active_run_ids: dict[str, str] = {}
        self._text_buffers: dict[tuple[str, str], dict[str, Any]] = {}
        # 工具调用须等 RESULT 才完整；START/ARGS 片段先缓冲，再成对写入。
        self._tool_call_buffers: dict[tuple[str, str], dict[str, Any]] = {}
        self._reasoning_buffers: dict[tuple[str, str], dict[str, Any]] = {}
        # events 仅为内存投影；这个游标标记已追加到 history.jsonl 的位置。
        self._persisted_event_counts: dict[str, int] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        title: str | None = None,
        session_id: str | None = None,
        source: str = "interactive",
        source_ref: str | None = None,
    ) -> SessionRecord:
        """创建新会话并写入存储后端。"""
        settings = await get_or_init_settings()
        title = title or settings.default_session_title
        session = SessionRecord(
            id=session_id or str(uuid.uuid4()), title=title,
            source=source, source_ref=source_ref,
        )
        async with self._lock:
            self._sessions[session.id] = session
            await self._ensure_workspace(session.id)
            await self._rewrite_history_locked(session)
            self._persisted_event_counts[session.id] = 0
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
                [session for session in self._sessions.values() if session.source == "interactive"],
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

    async def mark_source(
        self, session_id: str, source: str, source_ref: str | None = None
    ) -> SessionRecord | None:
        """Persistently classify an existing session before catalogs expose it."""
        await self._ensure_loaded()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.source = source
            session.source_ref = source_ref
            await self._persist(session)
            await self._context_store.validated(session.id, session.history_records)
            return session

    async def update(
        self,
        session_id: str,
        messages: list[ChatMessage],
        trace: list[str] | None = None,
        tasks: list[str] | None = None,
        thinking: list[dict] | None = None,
        events: list[dict] | None = None,
    ) -> SessionRecord:
        """整体替换会话状态并持久化。"""
        settings = await get_or_init_settings()
        async with self._lock:
            session = self._sessions[session_id]
            migrated = migrate_session_record({
                **self._record_to_payload(session),
                "messages": [
                    message.model_dump(mode="json", by_alias=True) for message in messages
                ],
                "events": coalesce_durable_events(events or []),
            })
            session.history_records = migrated.history_records
            session.next_history_seq = len(migrated.history_records) + 1
            session.events = events_from_records(session.history_records)
            session.messages = messages_from_records(session.history_records)
            self._persisted_event_counts[session_id] = len(session.events)
            await self._rewrite_history_locked(session)
            session.updated_at = datetime.now(timezone.utc)
            if session.title == settings.default_session_title:
                session.title = await self._derive_title(messages)
            await self._persist(session)
            await self._context_store.validated(session.id, session.history_records)
            return session

    async def save_run_start(
        self,
        session_id: str,
        messages: list[ChatMessage],
        *,
        run_id: str | None = None,
        mcp_server_ids: list[str],
        skill_ids: list[str],
        permission_mode: str = "default",
        model_id: str | None = None,
        agent_kind: str = "k_agent",
    ) -> SessionRecord:
        """在拿到会话锁之后调用：合并本轮 user 消息、记录可回滚 ID、清旧缓冲。"""
        settings = await get_or_init_settings()
        async with self._lock:
            session = self._sessions[session_id]
            self._active_run_ids.pop(session_id, None)
            self._cancelled_active_run_ids.pop(session_id, None)
            self._stopped_active_run_ids.pop(session_id, None)
            self._drop_session_buffers(session_id)
            existing_ids = {message.id for message in session.messages}
            session.messages = self._merge_messages(session.messages, messages)
            new_user_messages = [
                message for message in messages
                if message.role == "user" and message.id not in existing_ids
            ]
            for message in new_user_messages:
                serialized = message.model_dump(mode="json", by_alias=True)
                await self._append_history_locked(
                    session,
                    kind="input_message",
                    run_id=run_id,
                    message=serialized,
                )
                session.events.append({
                    "type": "input_message",
                    "runId": run_id,
                    "message": serialized,
                })
                # input_message 已作为独立 envelope 追加，不能在下个 AG-UI batch 重复写。
                self._persisted_event_counts[session_id] = len(session.events)
            if run_id:
                # cancel_run() only rolls back messages first accepted for this
                # run, so aborting a turn cannot delete earlier conversation.
                self._run_input_message_ids[(session_id, run_id)] = {
                    message.id
                    for message in messages
                    if message.role == "user" and message.id not in existing_ids
                }
            session.mcp_server_ids = list(dict.fromkeys(mcp_server_ids))
            session.skill_ids = list(dict.fromkeys(skill_ids))
            session.permission_mode = (
                "full_access" if permission_mode == "full_access" else "default"
            )
            session.model_id = model_id or session.model_id
            session.agent_kind = agent_kind
            session.updated_at = datetime.now(timezone.utc)
            if session.title == settings.default_session_title:
                session.title = await self._derive_title(session.messages)
            await self._persist(session)
            return session

    async def cancel_run(self, session_id: str, run_id: str) -> SessionRecord | None:
        """中止一轮运行：回滚本轮首次接受的 user 消息，并丢弃该 run 的半截缓冲/事件。"""
        settings = await get_or_init_settings()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            self._cancelled_run_ids.add((session_id, run_id))
            self._cancelled_active_run_ids[session_id] = run_id
            input_ids = self._run_input_message_ids.pop((session_id, run_id), set())
            if input_ids:
                session.messages = [
                    message for message in session.messages if message.id not in input_ids
                ]
            session.events = self._events_without_run(session.events, run_id)
            await self._append_history_locked(
                session,
                kind="history_mutation",
                run_id=run_id,
                mutation={"type": "remove_run", "runId": run_id},
            )
            self._persisted_event_counts[session_id] = len(session.events)
            if self._active_run_ids.get(session_id) == run_id:
                self._active_run_ids.pop(session_id, None)
            self._drop_run_buffers(session_id, run_id)
            session.updated_at = datetime.now(timezone.utc)
            if session.messages:
                session.title = await self._derive_title(session.messages)
            else:
                session.title = settings.default_session_title
            await self._persist(session)
            # remove_run 会改变有效历史投影；若覆盖了 boundary，旧摘要必须失效。
            await self._context_store.validated(session.id, session.history_records)
            return session

    async def stop_run(self, session_id: str, run_id: str) -> SessionRecord | None:
        """手动终止一轮运行：保留 user、已到达事件与非空 assistant 增量。

        终止边界由 Access Layer 持久化，而不是依赖已被浏览器 abort 的后端流继续
        发送事件。这样当前页面与刷新后的重放都停在同一个确定位置。
        """

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._run_has_terminal_event(session.events, run_id):
                return session

            key = (session_id, run_id)
            self._stopped_run_ids.add(key)
            self._stopped_active_run_ids[session_id] = run_id

            # TEXT_MESSAGE_END 不会在浏览器主动断流后到达，因此在这里把已有 delta
            # 封口为一条累计 CONTENT + 普通 assistant 消息。空占位不落盘。
            self._flush_incremental_events(session, session_id, run_id)
            self._seal_partial_text(session, session_id, run_id)

            self._drop_run_buffers(session_id, run_id)
            self._active_run_ids.pop(session_id, None)
            self._run_input_message_ids.pop(key, None)

            if not any(
                event.get("type") == "RUN_STARTED" and event.get("runId") == run_id
                for event in session.events
            ):
                session.events.append({
                    "type": "RUN_STARTED",
                    "threadId": session_id,
                    "runId": run_id,
                })
            session.events.append({
                "type": "RUN_FINISHED",
                "threadId": session_id,
                "runId": run_id,
                "result": {"status": "stopped", "stopped": True},
            })
            session.updated_at = datetime.now(timezone.utc)
            await self._append_pending_events_locked(session)
            await self._persist(session)
            return session

    async def append_event(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> SessionRecord:
        """累计流式 delta，并在结构边界写入完整 CONTENT/ARGS 与 assistant 消息。"""
        async with self._lock:
            session = self._sessions[session_id]
            event_type = str(event.get("type") or "")
            run_id = event.get("runId")
            incremental = event_type in INCREMENTAL_EVENT_TYPES or (
                event_type == "CUSTOM" and event.get("name") == "tool_output_delta"
            )

            def reject() -> SessionRecord:
                return session

            if isinstance(run_id, str) and (session_id, run_id) in self._stopped_run_ids:
                return reject()
            stopped_active_run_id = self._stopped_active_run_ids.get(session_id)
            if stopped_active_run_id and (not isinstance(run_id, str) or run_id == stopped_active_run_id):
                return reject()
            if isinstance(run_id, str) and (session_id, run_id) in self._cancelled_run_ids:
                if event_type == "RUN_STARTED":
                    self._cancelled_active_run_ids[session_id] = run_id
                if event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                    self._cancelled_active_run_ids.pop(session_id, None)
                return reject()
            cancelled_active_run_id = self._cancelled_active_run_ids.get(session_id)
            if cancelled_active_run_id and (not isinstance(run_id, str) or run_id == cancelled_active_run_id):
                if event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                    self._cancelled_active_run_ids.pop(session_id, None)
                return reject()

            if incremental:
                self._accumulate_incremental(session_id, event, event_type)
                return session

            # 非白名单事件既不能进入公开历史，也不能污染 Provider 投影。
            if not is_durable_event(event):
                return session
            session.events.append(event)
            # messages 只保存下一轮可输入的完整对话；events 在对应 END/RESULT
            # 时写入一条累计 delta，而不是每个 token。
            if event_type == "RUN_STARTED":
                run_id = event.get("runId")
                if isinstance(run_id, str) and run_id:
                    self._active_run_ids[session_id] = run_id
            elif event_type == "TEXT_MESSAGE_START":
                started = session.events.pop()
                self._flush_reasoning(session, session_id)
                session.events.append(started)
                message_id = event.get("messageId")
                if isinstance(message_id, str) and message_id:
                    self._text_buffers[(session_id, message_id)] = {
                        "content": "",
                        "createdAt": datetime.now(timezone.utc),
                        "runId": self._active_run_ids.get(session_id),
                    }
            elif event_type == "TEXT_MESSAGE_END":
                message_id = event.get("messageId")
                if isinstance(message_id, str) and message_id:
                    buffer = self._text_buffers.pop((session_id, message_id), None)
                    content = str(buffer["content"]) if buffer is not None else ""
                    if buffer is not None and not buffer.get("contentEmitted"):
                        self._emit_full_delta(
                            session, "TEXT_MESSAGE_CONTENT", {"messageId": message_id}, content
                        )
                        if content:
                            session.events.insert(-1, session.events.pop())
                    if buffer is not None and content.strip():
                        message = ChatMessage(
                            id=message_id,
                            role="assistant",
                            content=content,
                            createdAt=buffer["createdAt"],
                            meta=ChatMeta(runId=buffer.get("runId")),
                        )
                        session.messages = self._upsert_message(session.messages, message)
            elif event_type == "REASONING_MESSAGE_START":
                message_id = event.get("messageId")
                if isinstance(message_id, str) and message_id:
                    self._reasoning_buffers[(session_id, message_id)] = {
                        "content": "",
                        "runId": self._active_run_ids.get(session_id),
                    }
            elif event_type == "REASONING_MESSAGE_END":
                message_id = event.get("messageId")
                if isinstance(message_id, str) and message_id:
                    buffer = self._reasoning_buffers.pop((session_id, message_id), None)
                    content = str(buffer["content"]) if buffer is not None else ""
                    self._emit_full_delta(
                        session,
                        "REASONING_MESSAGE_CONTENT",
                        {"messageId": message_id},
                        content,
                    )
                    if content:
                        session.events.insert(-1, session.events.pop())
            elif event_type == "TOOL_CALL_START":
                started = session.events.pop()
                self._flush_reasoning(session, session_id)
                session.events.append(started)
                tool_call_id = event.get("toolCallId")
                if isinstance(tool_call_id, str) and tool_call_id:
                    self._tool_call_buffers[(session_id, tool_call_id)] = {
                        "name": str(event.get("toolCallName") or "tool"),
                        "arguments": "",
                        "createdAt": datetime.now(timezone.utc),
                        "runId": self._active_run_ids.get(session_id),
                    }
            elif event_type == "TOOL_CALL_END":
                tool_call_id = event.get("toolCallId")
                if isinstance(tool_call_id, str):
                    self._emit_tool_args(session, session_id, tool_call_id)
                    if self._last_event_is(session, "TOOL_CALL_ARGS"):
                        session.events.insert(-1, session.events.pop())
            elif event_type == "TOOL_CALL_RESULT":
                tool_call_id = event.get("toolCallId")
                if isinstance(tool_call_id, str):
                    self._emit_tool_args(session, session_id, tool_call_id)
                    if self._last_event_is(session, "TOOL_CALL_ARGS"):
                        session.events.insert(-1, session.events.pop())
                session.messages = self._append_tool_turn(
                    session_id, session.messages, event
                )
            elif event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                session.events.pop()
                finished_run_id = event.get("runId") if isinstance(event.get("runId"), str) else None
                self._flush_incremental_events(session, session_id, finished_run_id)
                self._seal_partial_text(session, session_id, finished_run_id)
                session.events.append(event)
                self._active_run_ids.pop(session_id, None)
                if isinstance(finished_run_id, str):
                    self._run_input_message_ids.pop((session_id, finished_run_id), None)
                # 未产生 RESULT 的 tool_calls 仍不进 messages，避免下一轮 Provider 拒收。
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
            if event_type in {
                "RUN_STARTED",
                "TEXT_MESSAGE_END",
                "REASONING_MESSAGE_END",
                "TOOL_CALL_END",
                "TOOL_CALL_RESULT",
                "RUN_FINISHED",
                "RUN_ERROR",
                "ACTIVITY_SNAPSHOT",
            }:
                await self._append_pending_events_locked(session)
                await self._persist(session)
            return session

    async def get(self, session_id: str) -> SessionRecord | None:
        """按 id 读取已加载会话；不存在返回 None（不隐式创建）。"""
        await self._ensure_loaded()
        async with self._lock:
            return self._sessions.get(session_id)

    async def persist_interrupt(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """在审批卡对浏览器可见前，原子保存其服务端 checkpoint。

        ``_checkpoint`` 是 Backend 与 Access Layer 的私有字段。它不会进入 SSE
        或普通 events，避免浏览器成为可执行恢复状态的信任来源。
        """

        content = event.get("content")
        if not isinstance(content, dict):
            raise ValueError("Approval activity content must be an object")
        interrupt_id = content.get("id")
        if not isinstance(interrupt_id, str) or not interrupt_id:
            raise ValueError("Approval activity is missing interrupt id")
        checkpoint = content.get("_checkpoint")
        if not isinstance(checkpoint, dict):
            raise ValueError("Approval activity is missing durable checkpoint")

        public_content = copy.deepcopy(content)
        public_content.pop("_checkpoint", None)
        public_event = copy.deepcopy(event)
        public_event["content"] = public_content
        now = datetime.now(timezone.utc).isoformat()

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            context_state = await self._context_store.validated(
                session_id, session.history_records
            )
            boundary = context_state.get("boundary")
            checkpoint = {
                **copy.deepcopy(checkpoint),
                "contextGeneration": int(context_state.get("generation", 0)),
                "boundaryId": boundary.get("id") if isinstance(boundary, dict) else None,
            }
            record = {
                "version": 1,
                "id": interrupt_id,
                "threadId": session_id,
                "runId": str(content.get("runId") or ""),
                "agentKind": str(content.get("agentKind") or "k_agent"),
                "category": str(content.get("category") or "tool"),
                "title": str(content.get("title") or "需要确认"),
                "message": str(content.get("message") or "请确认是否继续。"),
                "detail": copy.deepcopy(content.get("detail") or {}),
                "toolCallId": str(content.get("toolCallId") or interrupt_id),
                "requestHash": str(content.get("requestHash") or ""),
                "status": "pending",
                "checkpoint": checkpoint,
                "createdAt": now,
                "updatedAt": now,
                "resumeRunId": None,
                "decision": None,
            }
            existing = await self._read_approval_locked(session_id, interrupt_id)
            if existing is not None and existing.get("requestHash") != record["requestHash"]:
                raise ResumeConflictError("Interrupt id was reused with different arguments")
            if existing is None:
                await self._write_approval_locked(session_id, interrupt_id, record)
            if interrupt_id not in session.open_interrupt_ids:
                session.open_interrupt_ids.append(interrupt_id)
            session.updated_at = datetime.now(timezone.utc)
            await self._persist(session)
        return public_event

    async def list_open_interrupts(self, session_id: str) -> list[dict[str, Any]]:
        """返回前端可展示的开放审批；checkpoint 永远不离开 Access Layer。"""

        await self._ensure_loaded()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return []
            result: list[dict[str, Any]] = []
            for interrupt_id in session.open_interrupt_ids:
                record = await self._read_approval_locked(session_id, interrupt_id)
                if record is None:
                    continue
                result.append({
                    key: copy.deepcopy(value)
                    for key, value in record.items()
                    if key != "checkpoint"
                })
            return result

    async def prepare_resume(
        self,
        session_id: str,
        resume_entries: list[dict[str, Any]],
        *,
        resume_run_id: str,
        resume_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """完整校验并一次性认领开放 Interrupt，返回仅供 Backend 使用的记录。

        单 Access Layer worker 下，内存锁加独立文件原子替换构成 CAS。未来启用
        多 worker 前必须把这段认领迁到 SQLite/数据库事务。
        """

        await self._ensure_loaded()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            supplied = {
                str(entry.get("interruptId") or ""): entry for entry in resume_entries
            }
            expected = set(session.open_interrupt_ids)
            if not expected or set(supplied) != expected or "" in supplied:
                raise ResumeConflictError(
                    "Resume must resolve every open interrupt for this thread"
                )

            canonical = json.dumps(
                resume_entries,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            input_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
            intent_id = input_hash.removeprefix("sha256:")[:32]
            prior_intent = self._resume_intents.get((session_id, intent_id))
            if prior_intent is None and self._storage is not None:
                settings = await get_or_init_settings()
                raw = await self._storage.read_json(
                    f"{settings.session_storage_prefix}/{session_id}/resume-intents/{intent_id}.json"
                )
                prior_intent = raw if isinstance(raw, dict) else None
            if prior_intent is not None:
                if prior_intent.get("resumeRunId") != resume_run_id:
                    raise ResumeConflictError(
                        "This resume decision was already claimed by another run"
                    )
                return copy.deepcopy(prior_intent.get("interrupts") or [])

            records: list[dict[str, Any]] = []
            now = datetime.now(timezone.utc).isoformat()
            for interrupt_id in session.open_interrupt_ids:
                record = await self._read_approval_locked(session_id, interrupt_id)
                if record is None or record.get("status") not in {
                    "pending", "unknown_outcome", "resume_failed",
                }:
                    raise ResumeConflictError("Interrupt is no longer pending")
                checkpoint = record.get("checkpoint")
                context_state = await self._context_store.validated(
                    session_id, session.history_records
                )
                boundary = context_state.get("boundary")
                boundary_id = boundary.get("id") if isinstance(boundary, dict) else None
                if isinstance(checkpoint, dict) and (
                    checkpoint.get("contextGeneration", 0)
                    != context_state.get("generation", 0)
                    or checkpoint.get("boundaryId") != boundary_id
                ):
                    raise ResumeConflictError(
                        "Conversation context changed after this interrupt"
                    )
                stored_context = (
                    checkpoint.get("resumeContext")
                    if isinstance(checkpoint, dict)
                    else None
                )
                if stored_context is not None and stored_context != (resume_context or {}):
                    raise ResumeConflictError(
                        "Resume runtime differs from the interrupted run"
                    )
                entry = supplied[interrupt_id]
                status = entry.get("status")
                payload = entry.get("payload")
                if status not in {"resolved", "cancelled"}:
                    raise ResumeConflictError("Unsupported resume status")
                if status == "resolved":
                    if record.get("category") == "user_input":
                        detail = record.get("detail")
                        questions = (
                            detail.get("questions") if isinstance(detail, dict) else None
                        )
                        try:
                            normalized_answers = normalize_user_question_answers(
                                questions if isinstance(questions, list) else [],
                                payload.get("answers") if isinstance(payload, dict) else None,
                            )
                        except ValueError as exc:
                            raise ResumeConflictError(str(exc)) from exc
                        payload = {**payload, "answers": normalized_answers}
                        entry = {**entry, "payload": payload}
                    elif (
                        not isinstance(payload, dict)
                        or not isinstance(payload.get("approved"), bool)
                    ):
                        raise ResumeConflictError(
                            "Resolved approval payload must contain approved:boolean"
                        )
                if (
                    record.get("status") in {"unknown_outcome", "resume_failed"}
                    and (
                        not isinstance(payload, dict)
                        or payload.get("reconfirm") is not True
                    )
                ):
                    raise ResumeConflictError(
                        "Unknown tool outcome requires explicit reconfirmation"
                    )
                updated = copy.deepcopy(record)
                updated.update({
                    "status": "resuming",
                    "resumeRunId": resume_run_id,
                    "decision": copy.deepcopy(entry),
                    "updatedAt": now,
                })
                records.append(updated)

            intent = {
                "version": 1,
                "id": intent_id,
                "threadId": session_id,
                "resumeRunId": resume_run_id,
                "inputHash": input_hash,
                "status": "resuming",
                "interrupts": copy.deepcopy(records),
                "createdAt": now,
            }
            # Intent 先落盘，进程崩溃后仍能判断“是否已经认领”，不会重复执行。
            await self._write_resume_intent_locked(session_id, intent_id, intent)
            for record in records:
                await self._write_approval_locked(session_id, record["id"], record)
            return records

    async def finish_resume(
        self,
        session_id: str,
        interrupt_ids: list[str],
        *,
        succeeded: bool,
        unknown_outcome: bool = False,
    ) -> None:
        """在 Resume Run 落下终态后关闭索引；失败保留卡片但标为可诊断状态。"""

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            now = datetime.now(timezone.utc).isoformat()
            for interrupt_id in interrupt_ids:
                record = await self._read_approval_locked(session_id, interrupt_id)
                if record is None:
                    continue
                decision = record.get("decision") or {}
                payload = decision.get("payload") if isinstance(decision, dict) else {}
                if unknown_outcome:
                    final_status = "unknown_outcome"
                elif not succeeded:
                    final_status = "resume_failed"
                elif decision.get("status") == "cancelled":
                    final_status = "cancelled"
                elif record.get("category") == "user_input":
                    final_status = "answered"
                    if isinstance(payload, dict):
                        record["answers"] = copy.deepcopy(payload.get("answers") or {})
                elif isinstance(payload, dict) and payload.get("approved") is True:
                    final_status = "approved"
                else:
                    final_status = "denied"
                record.update({"status": final_status, "updatedAt": now})
                await self._write_approval_locked(session_id, interrupt_id, record)
                session.events.append({
                    "type": "ACTIVITY_SNAPSHOT",
                    "messageId": interrupt_id,
                    "activityType": "approval",
                    "replace": True,
                    "content": {
                        key: copy.deepcopy(value)
                        for key, value in record.items()
                        if key not in {"checkpoint", "decision"}
                    },
                })
            if succeeded:
                closed = set(interrupt_ids)
                session.open_interrupt_ids = [
                    item for item in session.open_interrupt_ids if item not in closed
                ]
            session.updated_at = datetime.now(timezone.utc)
            await self._append_pending_events_locked(session)
            await self._persist(session)

    async def ensure_accepts_new_input(self, session_id: str) -> None:
        """阻止用户消息跨过一个仍需决定的工具边界。"""

        await self._ensure_loaded()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.open_interrupt_ids:
                raise OpenInterruptError(
                    "Resolve the pending approval before sending another message"
                )

    async def apply_private_control(
        self, session_id: str, name: str, value: dict[str, Any]
    ) -> None:
        """消费 Backend 私有控制记录；这些数据绝不进入公开 SSE/history。"""

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if name == "cli_session":
                kind = value.get("kind")
                provider_session_id = value.get("sessionId")
                if isinstance(kind, str) and kind and isinstance(provider_session_id, str):
                    session.cli_sessions = {**session.cli_sessions, kind: provider_session_id}
                    await self._persist(session)
                return
            # 旧 context_state 是机械 bullet 摘要。完整协议启用后它只能作为
            # 观测兼容帧存在，不能再写入持久 state。

    async def provider_context(
        self, session_id: str
    ) -> tuple[list[ChatMessage], str]:
        """校验 compact boundary 后返回 Provider 活动尾部与摘要。"""

        messages, state = await self.provider_context_state(session_id)
        summary = state.get("summary")
        return messages, (
            str(summary.get("text") or "") if isinstance(summary, dict) else ""
        )

    async def provider_context_state(
        self, session_id: str
    ) -> tuple[list[ChatMessage], dict[str, Any]]:
        """返回 active tail 与完整私有 state，供 Backend 预算和 CAS 提案使用。"""

        await self._ensure_loaded()
        async with self._lock:
            session = self._sessions[session_id]
            return await self._context_store.active_view(
                session_id, session.history_records
            )

    async def commit_context_compaction(
        self,
        session_id: str,
        proposal: dict[str, Any],
        continuation_checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """把 Backend 提案锚定到当前 durable history 并原子提交。"""

        async with self._lock:
            session = self._sessions[session_id]
            return await self._context_store.commit_compaction(
                session_id,
                session.history_records,
                proposal,
                continuation_checkpoint,
            )

    async def commit_context_patch(
        self, session_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            return await self._context_store.commit_patch(session_id, patch)

    async def clear_context_continuation(
        self, session_id: str, generation: int
    ) -> None:
        async with self._lock:
            await self._context_store.clear_pending(
                session_id, expected_generation=generation
            )

    async def record_context_failure(
        self, session_id: str, *, code: str, automatic: bool
    ) -> dict[str, Any]:
        async with self._lock:
            return await self._context_store.record_failure(
                session_id, code=code, automatic=automatic
            )

    async def context_status(self, session_id: str) -> dict[str, Any] | None:
        await self._ensure_loaded()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            state = await self._context_store.validated(
                session_id, session.history_records
            )
            return self._context_store.public_status(state)

    async def pending_context_continuations(self) -> list[tuple[str, dict[str, Any]]]:
        """列出重启后必须先恢复的 K Agent continuation 私有快照。"""

        await self._ensure_loaded()
        pending: list[tuple[str, dict[str, Any]]] = []
        async with self._lock:
            for session_id, session in self._sessions.items():
                state = await self._context_store.validated(
                    session_id, session.history_records
                )
                checkpoint = state.get("pendingContinuation")
                if isinstance(checkpoint, dict):
                    pending.append((session_id, copy.deepcopy(state)))
        return pending

    async def has_pending_context_continuation(self, session_id: str) -> bool:
        """新输入的执行锁内复查，防止越过尚未恢复完成的内部执行段。"""

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            state = await self._context_store.validated(
                session_id, session.history_records
            )
            return isinstance(state.get("pendingContinuation"), dict)

    async def clear_all_context_failures(self) -> None:
        """compact 模型配置变化时恢复所有会话的自动尝试资格。"""

        await self._ensure_loaded()
        async with self._lock:
            for session_id in self._sessions:
                await self._context_store.clear_failure(session_id)

    async def reset_context(self, session_id: str) -> bool:
        await self._ensure_loaded()
        async with self._lock:
            if session_id not in self._sessions:
                return False
            await self._context_store.delete(session_id)
            return True

    async def _read_context_state_locked(self, session_id: str) -> dict[str, Any] | None:
        state = await self._context_store.read(session_id)
        return state if state != empty_context_state(session_id) else None

    async def _read_approval_locked(
        self, session_id: str, interrupt_id: str
    ) -> dict[str, Any] | None:
        cached = self._approval_records.get((session_id, interrupt_id))
        if cached is not None:
            return copy.deepcopy(cached)
        if self._storage is None:
            return None
        settings = await get_or_init_settings()
        raw = await self._storage.read_json(
            f"{settings.session_storage_prefix}/{session_id}/approvals/{interrupt_id}.json"
        )
        if not isinstance(raw, dict):
            return None
        self._approval_records[(session_id, interrupt_id)] = copy.deepcopy(raw)
        return copy.deepcopy(raw)

    async def _write_approval_locked(
        self, session_id: str, interrupt_id: str, record: dict[str, Any]
    ) -> None:
        self._approval_records[(session_id, interrupt_id)] = copy.deepcopy(record)
        if self._storage is None:
            return
        settings = await get_or_init_settings()
        await self._storage.write_json(
            f"{settings.session_storage_prefix}/{session_id}/approvals/{interrupt_id}.json",
            record,
        )

    async def _write_resume_intent_locked(
        self, session_id: str, intent_id: str, intent: dict[str, Any]
    ) -> None:
        self._resume_intents[(session_id, intent_id)] = copy.deepcopy(intent)
        if self._storage is None:
            return
        settings = await get_or_init_settings()
        await self._storage.write_json(
            f"{settings.session_storage_prefix}/{session_id}/resume-intents/{intent_id}.json",
            intent,
        )

    async def fork_session(self, session_id: str) -> SessionRecord | None:
        """Clone a stable conversation and workspace into an independent branch.

        Provider-native CLI resume ids are intentionally not copied: reusing them
        would let Codex/Claude continue the source provider thread even though the
        Access Layer created a new branch. Persisted messages remain the branch's
        complete model context.
        """

        await self._ensure_loaded()
        settings = await get_or_init_settings()
        async with self._lock:
            source = self._sessions.get(session_id)
            if source is None:
                return None
            if self._session_is_active(session_id):
                raise SessionBusyError("Cannot branch a session while it is running")

            branch_id = str(uuid.uuid4())
            branch = SessionRecord(
                id=branch_id,
                title=self._next_branch_title(source.title),
                messages=[message.model_copy(deep=True) for message in source.messages],
                events=self._events_for_branch(source.events, session_id, branch_id),
                history_records=[],
                mcp_server_ids=(copy.deepcopy(source.mcp_server_ids) if source.mcp_server_ids is not None else None),
                skill_ids=(copy.deepcopy(source.skill_ids) if source.skill_ids is not None else None),
                permission_mode=source.permission_mode,
                model_id=source.model_id,
                agent_kind=source.agent_kind,
                source="interactive",
                source_ref=session_id,
            )

            if self._storage is not None:
                source_workspace = self._storage.resolve(
                    f"{settings.session_storage_prefix}/{session_id}/workspace"
                )
                branch_workspace = self._storage.resolve(
                    f"{settings.session_storage_prefix}/{branch_id}/workspace"
                )
                await asyncio.to_thread(
                    self._copy_workspace_tree, source_workspace, branch_workspace
                )
            self._sessions[branch_id] = branch
            try:
                # 分支复制同一事实流但重新分配 seq/sessionId，避免共享可变历史文件。
                for record in source.history_records:
                    cloned = copy.deepcopy(record)
                    cloned["sessionId"] = branch_id
                    for cloned_event in cloned.get("events", []):
                        if isinstance(cloned_event, dict) and cloned_event.get("threadId") == session_id:
                            cloned_event["threadId"] = branch_id
                    if cloned.get("runId") is None:
                        cloned["runId"] = record.get("runId")
                    branch.history_records.append(cloned)
                branch.next_history_seq = max(
                    (int(record.get("seq") or 0) for record in branch.history_records),
                    default=0,
                ) + 1
                await self._rewrite_history_locked(branch)
                context_state = await self._read_context_state_locked(source.id)
                if isinstance(context_state, dict):
                    boundary_payload = context_state.get("boundary")
                    boundary = (
                        boundary_payload.get("coveredThroughSeq")
                        if isinstance(boundary_payload, dict)
                        else None
                    )
                    digest = (
                        boundary_payload.get("coveredPrefixDigest")
                        if isinstance(boundary_payload, dict)
                        else None
                    )
                    # 分支拷贝了同一事实前缀时才继承活动摘要；校验
                    # 失败则只保留完整 history，下轮可自动重建。
                    if (
                        isinstance(boundary, int)
                        and isinstance(digest, str)
                        and projected_prefix_digest(source.history_records, boundary) == digest
                        and projected_prefix_digest(branch.history_records, boundary) == digest
                    ):
                        branch_state = {
                            **copy.deepcopy(context_state),
                            "sessionId": branch_id,
                            "revision": 0,
                            "pendingContinuation": None,
                            "lastProposalId": None,
                        }
                        await self._storage.write_json(
                            f"{settings.session_storage_prefix}/{branch_id}/context/k_agent.json",
                            branch_state,
                        )
                self._persisted_event_counts[branch_id] = len(branch.events)
                await self._persist(branch)
            except Exception:
                self._sessions.pop(branch_id, None)
                if self._storage is not None:
                    branch_bundle = self._storage.resolve(
                        f"{settings.session_storage_prefix}/{branch_id}"
                    )
                    await asyncio.to_thread(shutil.rmtree, branch_bundle, True)
                raise
            return branch

    def _next_branch_title(self, source_title: str) -> str:
        """Return a stable numeric sibling name such as title（2）, title（3）."""

        base_title = re.sub(r"(?:（\d+）)+$", "", source_title).rstrip()
        base_title = base_title or source_title
        occupied = {1}
        numeric_pattern = re.compile(rf"^{re.escape(base_title)}（(\d+)）$")
        for session in self._sessions.values():
            title = session.title.rstrip()
            numeric_match = numeric_pattern.fullmatch(title)
            if numeric_match:
                occupied.add(int(numeric_match.group(1)))
        return f"{base_title}（{max(occupied) + 1}）"

    async def delete_session(self, session_id: str) -> bool:
        """Delete one complete session bundle after rejecting active-run races."""

        await self._ensure_loaded()
        settings = await get_or_init_settings()
        async with self._lock:
            if session_id not in self._sessions:
                return False
            if self._session_is_active(session_id):
                raise SessionBusyError("Cannot delete a session while it is running")
            if self._storage is not None:
                bundle = self._storage.resolve(
                    f"{settings.session_storage_prefix}/{session_id}"
                )
                if await asyncio.to_thread(bundle.exists):
                    await asyncio.to_thread(shutil.rmtree, bundle)
            self._sessions.pop(session_id, None)
            self._drop_session_buffers(session_id)
            self._active_run_ids.pop(session_id, None)
            self._cancelled_active_run_ids.pop(session_id, None)
            self._stopped_active_run_ids.pop(session_id, None)
            self._persisted_event_counts.pop(session_id, None)
            self._run_input_message_ids = {
                key: value
                for key, value in self._run_input_message_ids.items()
                if key[0] != session_id
            }
            self._cancelled_run_ids = {
                key for key in self._cancelled_run_ids if key[0] != session_id
            }
            self._stopped_run_ids = {
                key for key in self._stopped_run_ids if key[0] != session_id
            }
            self._approval_records = {
                key: value
                for key, value in self._approval_records.items()
                if key[0] != session_id
            }
            self._resume_intents = {
                key: value
                for key, value in self._resume_intents.items()
                if key[0] != session_id
            }
            return True

    def _session_is_active(self, session_id: str) -> bool:
        """Treat any active run or unfinished projection buffer as busy."""

        return (
            session_id in self._active_run_ids
            or any(key[0] == session_id for key in self._text_buffers)
            or any(key[0] == session_id for key in self._tool_call_buffers)
            or any(key[0] == session_id for key in self._reasoning_buffers)
        )

    @staticmethod
    def _events_for_branch(
        events: list[dict[str, Any]], source_id: str, branch_id: str
    ) -> list[dict[str, Any]]:
        """Retarget only session identity fields; user/model text stays byte-for-byte."""

        cloned = copy.deepcopy(events)
        for event in cloned:
            if event.get("threadId") == source_id:
                event["threadId"] = branch_id
            for field in ("value", "content"):
                nested = event.get(field)
                if isinstance(nested, dict) and nested.get("threadId") == source_id:
                    nested["threadId"] = branch_id
        return cloned

    @staticmethod
    def _copy_workspace_tree(source: Path, destination: Path) -> None:
        """Copy the branch workspace while preserving symlinks instead of following them."""

        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        else:
            destination.mkdir(parents=True, exist_ok=True)

    def _accumulate_incremental(
        self, session_id: str, event: dict[str, Any], event_type: str
    ) -> None:
        """SSE 增量只进内存缓冲，不写入 events。"""

        if event_type == "CUSTOM":
            return
        run_id = self._active_run_ids.get(session_id)
        if event_type == "TEXT_MESSAGE_CONTENT":
            message_id = event.get("messageId")
            delta = event.get("delta")
            if isinstance(message_id, str) and message_id and isinstance(delta, str):
                buffer = self._text_buffers.setdefault(
                    (session_id, message_id),
                    {"content": "", "createdAt": datetime.now(timezone.utc), "runId": run_id},
                )
                buffer["content"] = str(buffer["content"]) + delta
            return
        if event_type == "TOOL_CALL_ARGS":
            tool_call_id = event.get("toolCallId")
            delta = event.get("delta")
            if isinstance(tool_call_id, str) and isinstance(delta, str):
                buffer = self._tool_call_buffers.get((session_id, tool_call_id))
                if buffer is not None:
                    buffer["arguments"] = str(buffer["arguments"]) + delta
            return
        if event_type == "REASONING_MESSAGE_CONTENT":
            message_id = event.get("messageId")
            delta = event.get("delta")
            if isinstance(message_id, str) and message_id and isinstance(delta, str):
                buffer = self._reasoning_buffers.setdefault(
                    (session_id, message_id), {"content": "", "runId": run_id}
                )
                buffer["content"] = str(buffer["content"]) + delta
            return
    @staticmethod
    def _emit_full_delta(
        session: SessionRecord,
        event_type: str,
        extra: dict[str, Any],
        content: str,
    ) -> None:
        if not content:
            return
        session.events.append({"type": event_type, **extra, "delta": content})

    def _emit_tool_args(
        self, session: SessionRecord, session_id: str, tool_call_id: str
    ) -> None:
        buffer = self._tool_call_buffers.get((session_id, tool_call_id))
        if buffer is None or buffer.get("argsEmitted"):
            return
        session.events.append({
            "type": "TOOL_CALL_ARGS",
            "toolCallId": tool_call_id,
            "delta": str(buffer.get("arguments") or ""),
        })
        buffer["argsEmitted"] = True

    @staticmethod
    def _last_event_is(session: SessionRecord, event_type: str) -> bool:
        return bool(session.events) and session.events[-1].get("type") == event_type

    def _seal_partial_text(
        self,
        session: SessionRecord,
        session_id: str,
        run_id: str | None,
    ) -> None:
        """RUN 终态是文本硬边界；断流时也要生成 END 和 Provider 消息。"""

        for (buffer_session_id, message_id), buffer in list(self._text_buffers.items()):
            if buffer_session_id != session_id:
                continue
            if run_id is not None and buffer.get("runId") != run_id:
                continue
            content = str(buffer.get("content") or "")
            if content.strip():
                session.events.append({
                    "type": "TEXT_MESSAGE_END",
                    "messageId": message_id,
                })
                session.messages = self._upsert_message(
                    session.messages,
                    ChatMessage(
                        id=message_id,
                        role="assistant",
                        content=content,
                        createdAt=buffer["createdAt"],
                        meta=ChatMeta(runId=buffer.get("runId") or run_id),
                    ),
                )
            self._text_buffers.pop((buffer_session_id, message_id), None)

    def _flush_incremental_events(
        self,
        session: SessionRecord,
        session_id: str,
        run_id: str | None,
    ) -> None:
        """把尚未封口的累计块写进 events，供 stop / interrupt / 终态落盘。"""

        def matches(buffer: dict[str, Any]) -> bool:
            return run_id is None or buffer.get("runId") == run_id

        for (buffer_session_id, message_id), buffer in list(self._text_buffers.items()):
            if buffer_session_id != session_id or not matches(buffer) or buffer.get("contentEmitted"):
                continue
            self._emit_full_delta(
                session,
                "TEXT_MESSAGE_CONTENT",
                {"messageId": message_id},
                str(buffer.get("content") or ""),
            )
            buffer["contentEmitted"] = True
        for (buffer_session_id, message_id), buffer in list(self._reasoning_buffers.items()):
            if buffer_session_id != session_id or not matches(buffer) or buffer.get("contentEmitted"):
                continue
            self._emit_full_delta(
                session,
                "REASONING_MESSAGE_CONTENT",
                {"messageId": message_id},
                str(buffer.get("content") or ""),
            )
            buffer["contentEmitted"] = True
        for (buffer_session_id, tool_call_id), buffer in list(self._tool_call_buffers.items()):
            if buffer_session_id != session_id or not matches(buffer):
                continue
            self._emit_tool_args(session, session_id, tool_call_id)

    def _flush_reasoning(
        self, session: SessionRecord, session_id: str
    ) -> None:
        """工具/正文开始前封口 reasoning 块，避免 CONTENT 跨边界。"""

        run_id = self._active_run_ids.get(session_id)
        for (buffer_session_id, message_id), buffer in list(self._reasoning_buffers.items()):
            if buffer_session_id != session_id:
                continue
            if run_id is not None and buffer.get("runId") not in {run_id, None}:
                continue
            if not buffer.get("contentEmitted"):
                self._emit_full_delta(
                    session,
                    "REASONING_MESSAGE_CONTENT",
                    {"messageId": message_id},
                    str(buffer.get("content") or ""),
                )
            self._reasoning_buffers.pop((buffer_session_id, message_id), None)

    def _drop_session_buffers(self, session_id: str) -> None:
        """Discard partial text and tool-call state left by a finished run."""

        for buffers in (
            self._text_buffers,
            self._tool_call_buffers,
            self._reasoning_buffers,
        ):
            for key in [key for key in buffers if key[0] == session_id]:
                buffers.pop(key, None)

    def _drop_run_buffers(self, session_id: str, run_id: str) -> None:
        """Discard partial text/tool buffers owned by one cancelled run."""

        for buffers in (
            self._text_buffers,
            self._tool_call_buffers,
            self._reasoning_buffers,
        ):
            for key, value in list(buffers.items()):
                if key[0] == session_id and value.get("runId") == run_id:
                    buffers.pop(key, None)

    @staticmethod
    def _run_has_terminal_event(events: list[dict[str, Any]], run_id: str) -> bool:
        """Return true only for a persisted terminal boundary of this exact run."""

        return any(
            event.get("type") in {"RUN_FINISHED", "RUN_ERROR"}
            and event.get("runId") == run_id
            for event in events
        )

    @staticmethod
    def _events_without_run(events: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
        """Remove the contiguous AG-UI segment emitted by one run."""

        filtered: list[dict[str, Any]] = []
        dropping = False
        for event in events:
            event_type = str(event.get("type") or "")
            event_run_id = event.get("runId")
            starts_target_run = event_type == "RUN_STARTED" and event_run_id == run_id
            belongs_to_target_run = event_run_id == run_id or dropping
            if starts_target_run:
                dropping = True
            if not belongs_to_target_run and not starts_target_run:
                filtered.append(event)
            if dropping and event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                dropping = False
        return filtered

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
            # 旧目录迁移失败只记日志；其余会话仍可正常进入索引。
            await asyncio.to_thread(migrate_all_sessions, root)
            for path in await self._storage.list(prefix, "session.json"):
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                # 只接受 sessions/{id}/session.json，排除 approvals/context 等 JSON。
                if len(relative.parts) != 2 or relative.name != "session.json":
                    continue
                try:
                    payload = await self._storage.read_json(str(path))
                    if isinstance(payload, dict):
                        history_key = f"{prefix}/{relative.parts[0]}/history.jsonl"
                        history_lines = await self._storage.read_text_range(history_key)
                        history_records = [
                            json.loads(line) for line in history_lines if line.strip()
                        ]
                        session = self._record_from_payload(payload, history_records)
                        self._sessions[session.id] = session
                        self._persisted_event_counts[session.id] = len(session.events)
                        await self._ensure_workspace(session.id)
                except Exception:
                    # One unreadable session file must not take down the whole
                    # session index and, with it, every other conversation.
                    logger.error("Skipping unreadable session file %s", path, exc_info=True)
            # 单 worker 重启意味着旧的 resuming 进程已经不存在；工具可能在崩溃前
            # 执行过，不能自动重试。转成 unknown_outcome，等待用户显式二次确认。
            for session in self._sessions.values():
                for interrupt_id in session.open_interrupt_ids:
                    record = await self._read_approval_locked(session.id, interrupt_id)
                    if record is None or record.get("status") != "resuming":
                        continue
                    record.update({
                        "status": "unknown_outcome",
                        "updatedAt": datetime.now(timezone.utc).isoformat(),
                    })
                    await self._write_approval_locked(session.id, interrupt_id, record)
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
        """只写会话元数据；对话事实由 append-only history.jsonl 独立承担。"""
        if self._storage is None:
            return
        settings = await get_or_init_settings()
        await self._ensure_workspace(session.id)
        await self._storage.write_json(
            f"{settings.session_storage_prefix}/{session.id}/session.json",
            self._record_to_payload(session),
        )

    async def _append_history_locked(
        self,
        session: SessionRecord,
        *,
        kind: str,
        run_id: str | None,
        events: list[dict[str, Any]] | None = None,
        message: dict[str, Any] | None = None,
        mutation: dict[str, Any] | None = None,
    ) -> None:
        record = make_record(
            seq=session.next_history_seq,
            session_id=session.id,
            run_id=run_id,
            kind=kind,
            events=events,
            message=message,
            mutation=mutation,
        )
        if self._storage is not None:
            settings = await get_or_init_settings()
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            await self._storage.append_text(
                f"{settings.session_storage_prefix}/{session.id}/history.jsonl",
                line,
            )
        session.history_records.append(record)
        session.next_history_seq += 1

    async def _append_pending_events_locked(self, session: SessionRecord) -> None:
        """把自上个持久化边界以来的事件作为一个原子 batch 追加。"""

        start = self._persisted_event_counts.get(session.id, 0)
        pending = [
            copy.deepcopy(event) for event in session.events[start:]
            if is_durable_event(event)
        ]
        self._persisted_event_counts[session.id] = len(session.events)
        if not pending:
            return
        run_id = next(
            (
                str(event.get("runId"))
                for event in pending
                if isinstance(event.get("runId"), str) and event.get("runId")
            ),
            self._active_run_ids.get(session.id),
        )
        await self._append_history_locked(
            session,
            kind="agui_event" if len(pending) == 1 else "agui_event_batch",
            run_id=run_id,
            events=pending,
        )

    async def _rewrite_history_locked(self, session: SessionRecord) -> None:
        """仅新分支/显式整体替换使用；正常运行永远走 append。"""

        if self._storage is None:
            return
        settings = await get_or_init_settings()
        content = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in session.history_records
        )
        await self._storage.write_text(
            f"{settings.session_storage_prefix}/{session.id}/history.jsonl",
            content,
        )

    @staticmethod
    def _record_to_payload(session: SessionRecord) -> dict[str, Any]:
        """序列化非对话元数据；禁止写 messages/events/trace/tasks/thinking。"""
        return {
            "schemaVersion": 1,
            "id": session.id,
            "title": session.title,
            "capabilities": (
                {
                    "mcpServerIds": session.mcp_server_ids,
                    "skillIds": session.skill_ids,
                    "permissionMode": session.permission_mode,
                }
                if session.mcp_server_ids is not None
                and session.skill_ids is not None
                else None
            ),
            "cliSessions": dict(session.cli_sessions),
            "modelId": session.model_id,
            "agentKind": session.agent_kind,
            "openInterruptIds": list(session.open_interrupt_ids),
            "source": session.source,
            "sourceRef": session.source_ref,
            "updatedAt": session.updated_at.isoformat(),
        }

    @staticmethod
    def _record_from_payload(
        payload: dict[str, Any], history_records: list[dict[str, Any]] | None = None
    ) -> SessionRecord:
        """从元数据与完整历史重建运行时投影。"""
        updated_at = payload.get("updatedAt") or payload.get("updated_at")
        history_records = list(history_records or [])
        messages = messages_from_records(history_records)
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
            events=events_from_records(history_records),
            history_records=history_records,
            next_history_seq=max(
                (int(record.get("seq") or 0) for record in history_records), default=0
            ) + 1,
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
            permission_mode=(
                "full_access"
                if isinstance(capabilities, dict)
                and capabilities.get("permissionMode") == "full_access"
                else "default"
            ),
            model_id=(str(payload["modelId"]) if payload.get("modelId") else None),
            agent_kind=str(payload.get("agentKind") or "k_agent"),
            cli_sessions=cli_sessions,
            open_interrupt_ids=[
                str(item)
                for item in payload.get("openInterruptIds", [])
                if isinstance(item, str) and item
            ],
            source=str(payload.get("source") or "interactive"),
            source_ref=(str(payload["sourceRef"]) if payload.get("sourceRef") is not None else None),
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
