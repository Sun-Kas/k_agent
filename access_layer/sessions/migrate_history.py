"""旧会话单文件到 session.json + history.jsonl 的幂等迁移器。"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from access_layer.sessions.history import (
    durable_events,
    encode_records,
    make_record,
    messages_from_records,
)
from access_layer.storage.file import write_text_atomic


logger = logging.getLogger("k_agent.access_layer.sessions.migration")


@dataclass(frozen=True, slots=True)
class MigratedSession:
    """纯转换结果，便于 fixture 精确断言元数据与 JSONL 行。"""

    metadata: dict[str, Any]
    history_records: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class MigrationReport:
    migrated: int = 0
    skipped: int = 0
    failed: int = 0


def migrate_session_record(payload: dict[str, Any]) -> MigratedSession:
    """纯函数：过滤旧观测事件、插入用户回合并生成带 seq 的 envelope。"""

    session_id = str(payload["id"])
    old_messages = [item for item in payload.get("messages", []) if isinstance(item, dict)]
    old_events = durable_events([
        item for item in payload.get("events", []) if isinstance(item, dict)
    ])
    first_covered = _first_event_covered_message_index(old_messages, old_events)
    # 旧 events 有时只保留最近窗口，messages 却仍有更早的工具对。
    # 这部分必须先补在事件窗口之前，不能统一追加到文件尾部。
    prefix_messages = old_messages[:first_covered] if first_covered else []
    window_messages = old_messages[first_covered:] if first_covered else old_messages
    user_by_run, leading_users, trailing_users = _assign_users_to_runs(window_messages, old_events)

    records: list[dict[str, Any]] = []
    seq = 0

    def append_record(**kwargs: Any) -> None:
        nonlocal seq
        seq += 1
        records.append(make_record(seq=seq, session_id=session_id, **kwargs))

    for message in prefix_messages:
        if message.get("role") == "user":
            append_record(run_id=_message_run_id(message), kind="input_message", message=message)
            continue
        fallback_events = _fallback_events_for_message(message)
        if fallback_events:
            append_record(
                run_id=_message_run_id(message),
                kind="agui_event_batch" if len(fallback_events) > 1 else "agui_event",
                events=fallback_events,
            )

    for message in leading_users:
        append_record(run_id=_message_run_id(message), kind="input_message", message=message)

    inserted_runs: set[str] = set()
    for event in old_events:
        run_id = str(event.get("runId") or "") or None
        if event.get("type") == "RUN_STARTED" and run_id:
            for message in user_by_run.get(run_id, []):
                append_record(run_id=run_id, kind="input_message", message=message)
            inserted_runs.add(run_id)
        append_record(
            run_id=run_id,
            kind="agui_event",
            events=[event],
        )

    for run_id, messages in user_by_run.items():
        if run_id in inserted_runs:
            continue
        for message in messages:
            append_record(run_id=run_id, kind="input_message", message=message)
    for message in trailing_users:
        append_record(run_id=_message_run_id(message), kind="input_message", message=message)

    capabilities = payload.get("capabilities")
    metadata = {
        "schemaVersion": 1,
        "id": session_id,
        "title": str(payload.get("title") or ""),
        "capabilities": capabilities if isinstance(capabilities, dict) else None,
        "cliSessions": dict(payload.get("cliSessions") or payload.get("cli_sessions") or {}),
        "openInterruptIds": [
            item for item in payload.get("openInterruptIds", [])
            if isinstance(item, str) and item
        ],
        "source": str(payload.get("source") or "interactive"),
        "sourceRef": payload.get("sourceRef"),
        "updatedAt": payload.get("updatedAt") or payload.get("updated_at"),
    }

    # 转换必须至少重建旧 user/assistant/tool 语义。某些非常老的记录没有 events，
    # 此时补成规范事件，避免升级后 UI 或 Provider 历史突然清空。
    projected = messages_from_records(records)
    projected_ids = {message.id for message in projected}
    projected_tool_calls = {
        call.id for message in projected for call in message.tool_calls
    }
    projected_tool_results = {
        message.meta.tool_call_id
        for message in projected
        if message.role == "tool" and message.meta and message.meta.tool_call_id
    }
    for message in old_messages:
        message_id = message.get("id")
        if not isinstance(message_id, str):
            continue
        role = message.get("role")
        if role == "user" and message_id not in projected_ids:
            append_record(run_id=_message_run_id(message), kind="input_message", message=message)
            projected_ids.add(message_id)
        elif role == "assistant":
            run_id = _message_run_id(message)
            fallback_events: list[dict[str, Any]] = []
            content = str(message.get("content") or "")
            if message_id not in projected_ids and content.strip():
                fallback_events.extend([
                    {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"},
                    {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": content},
                    {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                ])
                projected_ids.add(message_id)
            for call in message.get("toolCalls", []):
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id or call_id in projected_tool_calls:
                    continue
                fallback_events.extend([
                    {
                        "type": "TOOL_CALL_START",
                        "toolCallId": call_id,
                        "toolCallName": str(call.get("name") or "tool"),
                    },
                    {
                        "type": "TOOL_CALL_ARGS",
                        "toolCallId": call_id,
                        "delta": str(call.get("arguments") or ""),
                    },
                    {"type": "TOOL_CALL_END", "toolCallId": call_id},
                ])
                projected_tool_calls.add(call_id)
            if fallback_events:
                append_record(run_id=run_id, kind="agui_event_batch", events=fallback_events)
        elif role == "tool":
            meta = message.get("meta")
            call_id = str(meta.get("toolCallId") or "") if isinstance(meta, dict) else ""
            if call_id and call_id not in projected_tool_results:
                append_record(run_id=_message_run_id(message), kind="agui_event", events=[{
                    "type": "TOOL_CALL_RESULT",
                    "toolCallId": call_id,
                    "messageId": message_id,
                    "content": str(message.get("content") or ""),
                }])
                projected_tool_results.add(call_id)
    return MigratedSession(metadata=metadata, history_records=records)


def migrate_session_dir(session_dir: Path, *, backup: bool = True) -> bool:
    """迁移一个目录；新形态跳过，失败前不改旧文件。"""

    session_dir = session_dir.resolve()
    target_metadata = session_dir / "session.json"
    target_history = session_dir / "history.jsonl"
    legacy = session_dir / f"{session_dir.name}.json"
    if target_metadata.is_file() and target_history.is_file():
        metadata = json.loads(target_metadata.read_text(encoding="utf-8"))
        if (
            isinstance(metadata, dict)
            and "events" not in metadata
            and not legacy.is_file()
        ):
            return False

    if not legacy.is_file():
        return False
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Legacy session is not an object: {legacy}")
    migrated = migrate_session_record(payload)
    validate_migration(payload, migrated.history_records)

    write_text_atomic(target_history, encode_records(migrated.history_records))
    write_text_atomic(
        target_metadata,
        json.dumps(migrated.metadata, ensure_ascii=False, indent=2) + "\n",
    )
    # 写完再读回并投影一次，验证通过后才移动旧文件。
    parsed = [
        json.loads(line) for line in target_history.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    validate_migration(payload, parsed)
    if backup:
        backup_path = legacy.with_suffix(legacy.suffix + ".bak")
        if not backup_path.exists():
            shutil.move(str(legacy), str(backup_path))
    else:
        legacy.unlink()
    return True


def repair_session_dir_from_backup(session_dir: Path) -> bool:
    """用只读 `.json.bak` 重建已迁移目标，用于升级迁移逻辑。"""

    session_dir = session_dir.resolve()
    backup_path = session_dir / f"{session_dir.name}.json.bak"
    if not backup_path.is_file():
        return False
    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Legacy backup is not an object: {backup_path}")
    migrated = migrate_session_record(payload)
    validate_migration(payload, migrated.history_records)
    write_text_atomic(session_dir / "history.jsonl", encode_records(migrated.history_records))
    write_text_atomic(
        session_dir / "session.json",
        json.dumps(migrated.metadata, ensure_ascii=False, indent=2) + "\n",
    )
    return True


def migrate_all_sessions(sessions_root: Path, *, backup: bool = True) -> MigrationReport:
    """逐目录迁移并隔离错误，一个坏会话不会阻塞整个索引。"""

    report = MigrationReport()
    if not sessions_root.is_dir():
        return report
    migrated = skipped = failed = 0
    for session_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
        try:
            if migrate_session_dir(session_dir, backup=backup):
                migrated += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
            logger.error("Failed to migrate session directory %s", session_dir, exc_info=True)
    return MigrationReport(migrated=migrated, skipped=skipped, failed=failed)


def repair_all_sessions(sessions_root: Path) -> MigrationReport:
    """批量重建有备份的新形态会话；每个目录独立失败。"""

    if not sessions_root.is_dir():
        return MigrationReport()
    migrated = skipped = failed = 0
    for session_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
        try:
            if repair_session_dir_from_backup(session_dir):
                migrated += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
            logger.error("Failed to repair session directory %s", session_dir, exc_info=True)
    return MigrationReport(migrated=migrated, skipped=skipped, failed=failed)


def validate_migration(payload: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """校验旧 messages 里的用户、助手正文和工具对均未丢失。

    顺序以旧 events 为真相源；某些旧 messages 本身已与事件窗口顺序不一致。
    """

    old = _message_semantics([
        item for item in payload.get("messages", []) if isinstance(item, dict)
    ])
    projected = [
        message.model_dump(mode="json", by_alias=True)
        for message in messages_from_records(records)
    ]
    current = _message_semantics(projected)
    unmatched = list(current)
    for index, expected in enumerate(old):
        try:
            found = unmatched.index(expected)
        except ValueError:
            raise ValueError(
                "Migrated history does not preserve message semantics "
                f"at item {index}: old={len(old)} projected={len(current)}"
            ) from None
        unmatched.pop(found)


def _message_semantics(messages: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    semantics: list[tuple[Any, ...]] = []
    for message in messages:
        role = message.get("role")
        message_id = str(message.get("id") or "")
        content = str(message.get("content") or "")
        if role == "user":
            semantics.append(("user", message_id, content, message.get("attachments") or []))
        elif role == "assistant":
            if content.strip():
                semantics.append(("assistant", message_id, content))
            for call in message.get("toolCalls", []):
                if isinstance(call, dict) and call.get("id"):
                    semantics.append((
                        "tool_call",
                        str(call["id"]),
                        str(call.get("name") or "tool"),
                        str(call.get("arguments") or ""),
                    ))
        elif role == "tool":
            meta = message.get("meta")
            call_id = str(meta.get("toolCallId") or "") if isinstance(meta, dict) else ""
            if call_id:
                semantics.append(("tool_result", call_id, message_id, content))
    return semantics


def _assign_users_to_runs(
    messages: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """旧 user 通常没有 runId；用其后首条带 runId 的模型消息稳定归属。"""

    event_run_ids = [
        str(event.get("runId")) for event in events
        if event.get("type") == "RUN_STARTED" and event.get("runId")
    ]
    by_run: dict[str, list[dict[str, Any]]] = {}
    pending: list[dict[str, Any]] = []
    leading: list[dict[str, Any]] = []
    seen_assigned = False
    for message in messages:
        if message.get("role") == "user":
            pending.append(message)
            continue
        run_id = _message_run_id(message)
        if pending and run_id:
            by_run.setdefault(run_id, []).extend(pending)
            pending = []
            seen_assigned = True
    if pending and event_run_ids:
        unused = [run_id for run_id in event_run_ids if run_id not in by_run]
        if unused:
            by_run.setdefault(unused[-1], []).extend(pending)
            pending = []
    if not seen_assigned and pending and not event_run_ids:
        leading, pending = pending, []
    return by_run, leading, pending


def _first_event_covered_message_index(
    messages: list[dict[str, Any]], events: list[dict[str, Any]]
) -> int | None:
    """定位旧事件窗口在完整 messages 中的起点。"""

    text_ids = {
        event.get("messageId") for event in events
        if event.get("type") in {"TEXT_MESSAGE_START", "TEXT_MESSAGE_END"}
    }
    tool_ids = {
        event.get("toolCallId") for event in events
        if str(event.get("type") or "").startswith("TOOL_CALL_")
    }
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            if message.get("id") in text_ids:
                return index
            if any(
                isinstance(call, dict) and call.get("id") in tool_ids
                for call in message.get("toolCalls", [])
            ):
                return index
        if message.get("role") == "tool":
            meta = message.get("meta")
            if isinstance(meta, dict) and meta.get("toolCallId") in tool_ids:
                return index
    return None


def _fallback_events_for_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """将没有历史事件的旧 assistant/tool 消息转成标准 AG-UI 事实。"""

    events: list[dict[str, Any]] = []
    role = message.get("role")
    message_id = str(message.get("id") or "")
    content = str(message.get("content") or "")
    if role == "assistant" and message_id and content.strip():
        events.extend([
            {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": content},
            {"type": "TEXT_MESSAGE_END", "messageId": message_id},
        ])
    if role == "assistant":
        for call in message.get("toolCalls", []):
            if not isinstance(call, dict) or not call.get("id"):
                continue
            call_id = str(call["id"])
            events.extend([
                {"type": "TOOL_CALL_START", "toolCallId": call_id, "toolCallName": str(call.get("name") or "tool")},
                {"type": "TOOL_CALL_ARGS", "toolCallId": call_id, "delta": str(call.get("arguments") or "")},
                {"type": "TOOL_CALL_END", "toolCallId": call_id},
            ])
    if role == "tool" and message_id:
        meta = message.get("meta")
        call_id = str(meta.get("toolCallId") or "") if isinstance(meta, dict) else ""
        if call_id:
            events.append({
                "type": "TOOL_CALL_RESULT",
                "toolCallId": call_id,
                "messageId": message_id,
                "content": content,
            })
    return events


def _message_run_id(message: dict[str, Any]) -> str | None:
    meta = message.get("meta")
    value = meta.get("runId") if isinstance(meta, dict) else None
    return str(value) if isinstance(value, str) and value else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate K Agent session history")
    parser.add_argument("sessions_root", type=Path)
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument(
        "--repair-from-backup",
        action="store_true",
        help="rebuild session.json/history.jsonl from retained .json.bak files",
    )
    args = parser.parse_args()
    report = (
        repair_all_sessions(args.sessions_root)
        if args.repair_from_backup
        else migrate_all_sessions(args.sessions_root, backup=not args.no_backup)
    )
    print(json.dumps({
        "migrated": report.migrated,
        "skipped": report.skipped,
        "failed": report.failed,
    }, ensure_ascii=False))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
