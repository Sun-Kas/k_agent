"""追加式会话历史协议与 Provider 消息投影。

history.jsonl 是 UI 和审计的唯一事实源；本模块只做确定性转换，不读取 Backend、
Prompt 或 Provider 配置。这样 compact state 丢失时仍能从历史重建模型输入。
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from access_layer.schemas import ChatMessage, ChatMeta, ToolCallRecord
from access_layer.sessions.durable_events import coalesce_durable_events


PUBLIC_EVENT_TYPES = frozenset({
    "RUN_STARTED",
    "RUN_FINISHED",
    "RUN_ERROR",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "REASONING_START",
    "REASONING_MESSAGE_START",
    "REASONING_MESSAGE_CONTENT",
    "REASONING_MESSAGE_END",
    "REASONING_END",
    "TOOL_CALL_START",
    "TOOL_CALL_ARGS",
    "TOOL_CALL_END",
    "TOOL_CALL_RESULT",
    "ACTIVITY_SNAPSHOT",
    # 这两类快照只允许实时透传，不能进入 history。
    "STATE_SNAPSHOT",
    "MESSAGES_SNAPSHOT",
})

DURABLE_EVENT_TYPES = PUBLIC_EVENT_TYPES - {"STATE_SNAPSHOT", "MESSAGES_SNAPSHOT"}
PRIVATE_CONTROL_NAMES = frozenset({"cli_session", "context_state"})


def is_public_event(event: dict[str, Any]) -> bool:
    """公开 SSE 白名单；CUSTOM 只承载无正文的产品提示或实时 stdout。"""

    event_type = str(event.get("type") or "")
    return event_type in PUBLIC_EVENT_TYPES or (
        event_type == "CUSTOM"
        and event.get("name") in {"tool_output_delta", "context_warning", "context_compacted"}
    )


def is_durable_event(event: dict[str, Any]) -> bool:
    """对话历史白名单，明确排除协议快照与实时 stdout。"""

    return str(event.get("type") or "") in DURABLE_EVENT_TYPES or (
        event.get("type") == "CUSTOM" and event.get("name") == "context_compacted"
    )


def make_record(
    *,
    seq: int,
    session_id: str,
    run_id: str | None,
    kind: str,
    events: list[dict[str, Any]] | None = None,
    message: dict[str, Any] | None = None,
    mutation: dict[str, Any] | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """构造统一 envelope；可选字段按 kind 严格分离。"""

    record: dict[str, Any] = {
        "schemaVersion": 1,
        "seq": seq,
        "sessionId": session_id,
        "runId": run_id,
        "kind": kind,
        "recordedAt": recorded_at or datetime.now(timezone.utc).isoformat(),
    }
    if events is not None:
        record["events"] = copy.deepcopy(events)
    if message is not None:
        record["message"] = copy.deepcopy(message)
    if mutation is not None:
        record["mutation"] = copy.deepcopy(mutation)
    return record


def encode_records(records: Iterable[dict[str, Any]]) -> str:
    """把完整 envelope 批次编码为 JSONL。"""

    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


def decode_records(lines: Iterable[str]) -> list[dict[str, Any]]:
    """读取 JSONL，坏行显式失败，避免静默产生残缺 transcript。"""

    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError("History record must be an object")
        records.append(payload)
    return records


def visible_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """应用 append-only tombstone；历史原行不改写，派生视图隐藏被取消 run。"""

    removed_runs: set[str] = set()
    replaced_messages: dict[str, dict[str, Any]] = {}
    deleted_messages: set[str] = set()
    for record in records:
        if record.get("kind") != "history_mutation":
            continue
        mutation = record.get("mutation")
        if not isinstance(mutation, dict):
            continue
        if mutation.get("type") == "remove_run" and isinstance(mutation.get("runId"), str):
            removed_runs.add(mutation["runId"])
        elif mutation.get("type") == "delete_message" and isinstance(mutation.get("messageId"), str):
            deleted_messages.add(mutation["messageId"])
        elif mutation.get("type") == "replace_message":
            message = mutation.get("message")
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                replaced_messages[message["id"]] = message

    result: list[dict[str, Any]] = []
    for record in records:
        if record.get("kind") == "history_mutation" or record.get("runId") in removed_runs:
            continue
        current = copy.deepcopy(record)
        if current.get("kind") == "input_message":
            message = current.get("message")
            message_id = message.get("id") if isinstance(message, dict) else None
            if message_id in deleted_messages:
                continue
            if message_id in replaced_messages:
                current["message"] = copy.deepcopy(replaced_messages[message_id])
        result.append(current)
    return result


def events_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 envelope 展平为 UI 可重放事件，并保留 input_message 的原始位置。"""

    events: list[dict[str, Any]] = []
    for record in visible_records(records):
        kind = record.get("kind")
        if kind == "input_message" and isinstance(record.get("message"), dict):
            events.append({
                "type": "input_message",
                "runId": record.get("runId"),
                "message": copy.deepcopy(record["message"]),
            })
        elif kind in {"agui_event", "agui_event_batch"}:
            events.extend(
                copy.deepcopy(event)
                for event in record.get("events", [])
                if isinstance(event, dict)
            )
    return events


def messages_from_records(
    records: list[dict[str, Any]], *, through_seq: int | None = None
) -> list[ChatMessage]:
    """从对话事实流投影 Provider messages，不依赖 session.json 的旧 messages。"""

    selected = [
        record for record in visible_records(records)
        if through_seq is None or int(record.get("seq") or 0) <= through_seq
    ]
    messages: list[ChatMessage] = []
    text_buffers: dict[str, dict[str, Any]] = {}
    tool_buffers: dict[str, dict[str, Any]] = {}
    active_run_id: str | None = None

    for record in selected:
        run_id = record.get("runId") if isinstance(record.get("runId"), str) else active_run_id
        if record.get("kind") == "input_message" and isinstance(record.get("message"), dict):
            messages = _upsert_message(messages, ChatMessage.model_validate(record["message"]))
            continue
        for event in record.get("events", []):
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "RUN_STARTED":
                active_run_id = str(event.get("runId") or run_id or "") or None
            elif event_type == "TEXT_MESSAGE_START":
                message_id = str(event.get("messageId") or "")
                if message_id:
                    text_buffers[message_id] = {"content": "", "runId": run_id or active_run_id}
            elif event_type == "TEXT_MESSAGE_CONTENT":
                message_id = str(event.get("messageId") or "")
                if message_id:
                    buffer = text_buffers.setdefault(message_id, {"content": "", "runId": run_id or active_run_id})
                    buffer["content"] += str(event.get("delta") or "")
            elif event_type == "TEXT_MESSAGE_END":
                message_id = str(event.get("messageId") or "")
                buffer = text_buffers.pop(message_id, None)
                if buffer is not None and str(buffer["content"]).strip():
                    messages = _upsert_message(messages, ChatMessage(
                        id=message_id,
                        role="assistant",
                        content=str(buffer["content"]),
                        createdAt=_event_time(event, record),
                        meta=ChatMeta(runId=buffer.get("runId")),
                    ))
            elif event_type == "TOOL_CALL_START":
                call_id = str(event.get("toolCallId") or "")
                if call_id:
                    tool_buffers[call_id] = {
                        "name": str(event.get("toolCallName") or "tool"),
                        "arguments": "",
                        "runId": run_id or active_run_id,
                    }
            elif event_type == "TOOL_CALL_ARGS":
                call_id = str(event.get("toolCallId") or "")
                if call_id in tool_buffers:
                    tool_buffers[call_id]["arguments"] += str(event.get("delta") or "")
            elif event_type == "TOOL_CALL_RESULT":
                call_id = str(event.get("toolCallId") or "")
                buffer = tool_buffers.pop(call_id, None)
                content = event.get("content")
                if buffer is None or not isinstance(content, str):
                    continue
                event_time = _event_time(event, record)
                messages = _upsert_message(messages, ChatMessage(
                    id=f"toolcall-{call_id}", role="assistant", content="",
                    createdAt=event_time,
                    meta=ChatMeta(toolName=buffer["name"], runId=buffer.get("runId")),
                    toolCalls=[ToolCallRecord(id=call_id, name=buffer["name"], arguments=buffer["arguments"])],
                ))
                messages = _upsert_message(messages, ChatMessage(
                    id=str(event.get("messageId") or f"toolresult-{call_id}"),
                    role="tool", content=content, createdAt=event_time,
                    meta=ChatMeta(toolName=buffer["name"], runId=buffer.get("runId"), toolCallId=call_id),
                ))
    return messages


def message_seq_index(records: list[dict[str, Any]]) -> dict[str, int]:
    """返回完整语义组结束时的 seq，compact boundary 不会落在半条流中。"""

    index: dict[str, int] = {}
    for record in visible_records(records):
        seq = int(record.get("seq") or 0)
        if record.get("kind") == "input_message" and isinstance(record.get("message"), dict):
            message_id = record["message"].get("id")
            if isinstance(message_id, str):
                index[message_id] = seq
        for event in record.get("events", []):
            if not isinstance(event, dict):
                continue
            if event.get("type") in {"TEXT_MESSAGE_END", "TOOL_CALL_RESULT"}:
                message_id = event.get("messageId")
                if isinstance(message_id, str):
                    index[message_id] = seq
            if event.get("type") == "TOOL_CALL_RESULT" and isinstance(event.get("toolCallId"), str):
                index[f"toolcall-{event['toolCallId']}"] = seq
    return index


def projected_prefix_digest(records: list[dict[str, Any]], through_seq: int) -> str:
    """摘要覆盖前缀的稳定校验值；编辑/取消会改变有效消息投影。"""

    payload = [
        message.model_dump(mode="json", by_alias=True)
        for message in messages_from_records(records, through_seq=through_seq)
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_time(event: dict[str, Any], record: dict[str, Any]) -> datetime:
    value = event.get("createdAt") or record.get("recordedAt")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _upsert_message(messages: list[ChatMessage], message: ChatMessage) -> list[ChatMessage]:
    for index, existing in enumerate(messages):
        if existing.id == message.id:
            return [*messages[:index], message, *messages[index + 1 :]]
    return [*messages, message]


def durable_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """迁移/批写共同使用的白名单与 delta 合并入口。"""

    normalized = normalize_legacy_events([
        event for event in events if isinstance(event, dict)
    ])
    return coalesce_durable_events([
        event for event in normalized if is_durable_event(event)
    ])


def normalize_legacy_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按旧 Thinking 生命周期生成稳定 ID，避免多个块被误合并。"""

    normalized: list[dict[str, Any]] = []
    reasoning_id: str | None = None
    step_id: str | None = None
    block_index = 0
    step_index = 0
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "CUSTOM" and event.get("name") in {
            "approval_request",
            "approval_resolved",
        }:
            value = event.get("value")
            if not isinstance(value, dict):
                continue
            content = copy.deepcopy(value)
            if event.get("name") == "approval_request":
                content.setdefault("status", "pending")
            approval_id = str(content.get("id") or "")
            if approval_id:
                normalized.append({
                    "type": "ACTIVITY_SNAPSHOT",
                    "messageId": approval_id,
                    "activityType": "approval",
                    "replace": True,
                    "content": content,
                })
            continue
        if event_type == "THINKING_START":
            block_index += 1
            raw = event.get("rawEvent")
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            reasoning_id = str(raw_id or event.get("messageId") or f"legacy-reasoning-{block_index}")
            step_id = None
        elif event_type == "THINKING_TEXT_MESSAGE_START":
            step_index += 1
            raw = event.get("rawEvent")
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            step_id = str(raw_id or event.get("messageId") or f"legacy-reasoning-step-{step_index}")

        current = normalize_legacy_event(event)
        if event_type in {"THINKING_START", "THINKING_END"} and reasoning_id:
            current["messageId"] = reasoning_id
        elif event_type in {
            "THINKING_TEXT_MESSAGE_START",
            "THINKING_TEXT_MESSAGE_CONTENT",
            "THINKING_TEXT_MESSAGE_END",
        }:
            current["messageId"] = step_id or reasoning_id or f"legacy-reasoning-step-{step_index or 1}"
        normalized.append(current)

        if event_type == "THINKING_TEXT_MESSAGE_END":
            step_id = None
        elif event_type == "THINKING_END":
            reasoning_id = None
            step_id = None
    return normalized


def normalize_legacy_event(event: dict[str, Any]) -> dict[str, Any]:
    """将旧 THINKING 名称规范成 REASONING；ID 由旧 rawEvent 稳定继承。"""

    normalized = copy.deepcopy(event)
    mapping = {
        "THINKING_START": "REASONING_START",
        "THINKING_TEXT_MESSAGE_START": "REASONING_MESSAGE_START",
        "THINKING_TEXT_MESSAGE_CONTENT": "REASONING_MESSAGE_CONTENT",
        "THINKING_TEXT_MESSAGE_END": "REASONING_MESSAGE_END",
        "THINKING_END": "REASONING_END",
    }
    event_type = str(normalized.get("type") or "")
    if event_type not in mapping:
        return normalized
    normalized["type"] = mapping[event_type]
    raw = normalized.get("rawEvent")
    raw_id = raw.get("id") if isinstance(raw, dict) else None
    normalized.setdefault("messageId", str(raw_id or normalized.get("messageId") or "legacy-reasoning"))
    if normalized["type"] == "REASONING_MESSAGE_START":
        normalized.setdefault("role", "reasoning")
    normalized.pop("title", None)
    return normalized
