"""会话持久化与 AG-UI 事件投影：Access Layer 拥有对话真相源。

在请求链路中的角色：
- `save_run_start`：在会话锁保护下写入本轮 user 消息
- `append_event`：消费后端 AG-UI 流，投影为可再输入的 messages，并按边界落盘
- `cancel_run`：撤销本轮用户消息与半截流状态
- `stop_run`：保留本轮已接收内容并写入稳定的手动终止边界

服务边界：
- 存储布局 `sessions/{id}/{id}.json` + 同级 `workspace/`；本层拥有，后端无状态
- 本类的 asyncio.Lock 只保护内存索引与落盘，不是 agent-run 互斥
  （互斥在 RequestConcurrencyLimiter）
- events 保留完整流；messages 只在 TEXT_MESSAGE_END / TOOL_CALL_RESULT 等
  结构边界写入完整内容，避免半截 delta 进入下一轮上下文
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

from backend.config import get_or_init_settings
from backend.api.schemas import ChatMessage, ChatMeta, SessionSummary, ToolCallRecord
from backend.storage import StorageBackend


logger = logging.getLogger("k_agent.access_layer.sessions")


class SessionBusyError(RuntimeError):
    """Raised when a destructive session operation races an active run."""


class OpenInterruptError(RuntimeError):
    """线程存在未解决 Interrupt，普通用户输入不能越过该恢复边界。"""


class ResumeConflictError(RuntimeError):
    """Resume 未完整覆盖开放 Interrupt，或与已经认领的决定冲突。"""


@dataclass(slots=True)
class SessionRecord:
    """由已接受 AG-UI 事件重建的持久化会话状态（messages / events / 能力选择等）。"""

    id: str
    title: str
    messages: list[ChatMessage] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    thinking: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    mcp_server_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    permission_mode: str = "default"
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
            return session

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
        run_id: str | None = None,
        mcp_server_ids: list[str],
        skill_ids: list[str],
        permission_mode: str = "default",
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
            if self._active_run_ids.get(session_id) == run_id:
                self._active_run_ids.pop(session_id, None)
            self._drop_run_buffers(session_id, run_id)
            session.updated_at = datetime.now(timezone.utc)
            if session.messages:
                session.title = await self._derive_title(session.messages)
            else:
                session.title = settings.default_session_title
            await self._persist(session)
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
            # 封口为普通 assistant 消息。空占位不落盘，避免刷新后出现空白气泡。
            for (buffer_session_id, message_id), buffer in list(self._text_buffers.items()):
                if buffer_session_id != session_id or buffer.get("runId") != run_id:
                    continue
                content = str(buffer.get("content") or "")
                if content.strip():
                    message = ChatMessage(
                        id=message_id,
                        role="assistant",
                        content=content,
                        createdAt=buffer["createdAt"],
                        meta=ChatMeta(runId=run_id),
                    )
                    session.messages = self._upsert_message(session.messages, message)
                self._text_buffers.pop((buffer_session_id, message_id), None)

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
            run_id = event.get("runId")
            if isinstance(run_id, str) and (session_id, run_id) in self._stopped_run_ids:
                session.events.pop()
                return session
            stopped_active_run_id = self._stopped_active_run_ids.get(session_id)
            if stopped_active_run_id and (not isinstance(run_id, str) or run_id == stopped_active_run_id):
                session.events.pop()
                return session
            if isinstance(run_id, str) and (session_id, run_id) in self._cancelled_run_ids:
                if event_type == "RUN_STARTED":
                    self._cancelled_active_run_ids[session_id] = run_id
                if event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                    self._cancelled_active_run_ids.pop(session_id, None)
                session.events.pop()
                return session
            cancelled_active_run_id = self._cancelled_active_run_ids.get(session_id)
            if cancelled_active_run_id and (not isinstance(run_id, str) or run_id == cancelled_active_run_id):
                if event_type in {"RUN_FINISHED", "RUN_ERROR"}:
                    self._cancelled_active_run_ids.pop(session_id, None)
                session.events.pop()
                return session

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
                run_id = event.get("runId")
                if isinstance(run_id, str):
                    self._run_input_message_ids.pop((session_id, run_id), None)
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
                "checkpoint": copy.deepcopy(checkpoint),
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
                if status == "resolved" and (
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
                trace=copy.deepcopy(source.trace),
                tasks=copy.deepcopy(source.tasks),
                thinking=copy.deepcopy(source.thinking),
                events=self._events_for_branch(source.events, session_id, branch_id),
                mcp_server_ids=(copy.deepcopy(source.mcp_server_ids) if source.mcp_server_ids is not None else None),
                skill_ids=(copy.deepcopy(source.skill_ids) if source.skill_ids is not None else None),
                permission_mode=source.permission_mode,
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

    def _drop_session_buffers(self, session_id: str) -> None:
        """Discard partial text and tool-call state left by a finished run."""

        for buffers in (self._text_buffers, self._tool_call_buffers):
            for key in [key for key in buffers if key[0] == session_id]:
                buffers.pop(key, None)

    def _drop_run_buffers(self, session_id: str, run_id: str) -> None:
        """Discard partial text/tool buffers owned by one cancelled run."""

        for buffers in (self._text_buffers, self._tool_call_buffers):
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
                    "permissionMode": session.permission_mode,
                }
                if session.mcp_server_ids is not None
                and session.skill_ids is not None
                else None
            ),
            "cliSessions": dict(session.cli_sessions),
            "openInterruptIds": list(session.open_interrupt_ids),
            "source": session.source,
            "sourceRef": session.source_ref,
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
            permission_mode=(
                "full_access"
                if isinstance(capabilities, dict)
                and capabilities.get("permissionMode") == "full_access"
                else "default"
            ),
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
