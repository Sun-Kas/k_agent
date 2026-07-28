from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ag_ui.core import (
    CustomEvent,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    ThinkingTextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from backend.api.schemas import ChatMessage


encoder = EventEncoder()


def encode_event(event: Any) -> str:
    return encoder.encode(event)


def to_chat_messages(messages: list[Any]) -> list[ChatMessage]:
    converted: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content", "")
            message_id = message.get("id")
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", "")
            message_id = getattr(message, "id", None)
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        converted.append(
            ChatMessage(
                id=str(message_id) if message_id else str(uuid.uuid4()),
                role=role,
                content=content,
                createdAt=datetime.now(timezone.utc),
            )
        )
    return converted


async def translate_agent_events(
    events: AsyncIterator[dict[str, Any]],
    thread_id: str,
    run_id: str,
    previous_messages: list[ChatMessage] | None = None,
) -> AsyncIterator[Any]:
    thinking_groups: list[dict[str, Any]] = []
    tool_activities: list[dict[str, Any]] = []
    text_activities: list[dict[str, Any]] = []
    active_thinking_group: dict[str, Any] | None = None
    active_thinking_step_id: str | None = None
    active_text_activity_id: str | None = None
    visible_text_offset = 0
    activity_sequence = 0

    def count_visible_text(content: str) -> int:
        """只统计可见字符，避免换行或空格把连续 thinking 误判成两段正文。"""
        return sum(1 for char in content if not char.isspace())

    def thinking_events(step: dict[str, Any]) -> list[Any]:
        nonlocal active_thinking_group, active_thinking_step_id, activity_sequence
        if step.get("phase") == "tool":
            return []

        events: list[Any] = []
        for group in thinking_groups:
            steps = group.get("steps", [])
            for index, existing in enumerate(steps):
                if existing.get("id") == step.get("id"):
                    previous_detail = str(existing.get("detail") or "")
                    steps[index] = step
                    # 已收到 THINKING_END 的组只更新持久化快照，绝不再向前端续写。
                    if group.get("closed") or active_thinking_step_id != step.get("id"):
                        return events
                    detail = str(step.get("detail") or "")
                    if detail.startswith(previous_detail):
                        delta = detail[len(previous_detail):]
                        if delta:
                            events.append(
                                ThinkingTextMessageContentEvent(
                                    delta=delta,
                                    raw_event={"id": step.get("id")},
                                )
                            )
                    else:
                        # 上游把占位说明替换为真实 thinking 正文时，用标准的
                        # text-message end/start 开启一条新文本流，不能私下覆盖。
                        events.append(
                            ThinkingTextMessageEndEvent(
                                raw_event={**existing, "status": "complete"}
                            )
                        )
                        events.append(ThinkingTextMessageStartEvent(raw_event=step))
                        if detail:
                            events.append(
                                ThinkingTextMessageContentEvent(
                                    delta=detail,
                                    raw_event={"id": step.get("id")},
                                )
                            )
                    if step.get("status") != "active":
                        events.append(ThinkingTextMessageEndEvent(raw_event=step))
                        active_thinking_step_id = None
                    return events

        if active_thinking_group is None or active_thinking_group.get("closed"):
            activity_sequence += 1
            active_thinking_group = {
                "id": str(uuid.uuid4()),
                "steps": [],
                "closed": False,
                "textStart": visible_text_offset,
                "textEnd": visible_text_offset,
                "sequence": activity_sequence,
            }
            thinking_groups.append(active_thinking_group)
            events.append(ThinkingStartEvent(title=step.get("title")))

        if active_thinking_step_id is not None:
            events.append(
                ThinkingTextMessageEndEvent(
                    raw_event={"id": active_thinking_step_id, "status": "complete"}
                )
            )
        active_thinking_step_id = str(step.get("id") or uuid.uuid4())
        normalized_step = {**step, "id": active_thinking_step_id}
        active_thinking_group["steps"].append(normalized_step)
        active_thinking_group["textEnd"] = visible_text_offset
        events.append(ThinkingTextMessageStartEvent(raw_event=normalized_step))
        detail = str(normalized_step.get("detail") or "")
        if detail:
            events.append(
                ThinkingTextMessageContentEvent(
                    delta=detail,
                    raw_event={"id": active_thinking_step_id},
                )
            )
        if normalized_step.get("status") != "active":
            events.append(ThinkingTextMessageEndEvent(raw_event=normalized_step))
            active_thinking_step_id = None
        return events

    def close_thinking_events() -> list[Any]:
        nonlocal active_thinking_group, active_thinking_step_id
        if active_thinking_group is None:
            return []
        events: list[Any] = []
        if active_thinking_step_id is not None:
            events.append(
                ThinkingTextMessageEndEvent(
                    raw_event={"id": active_thinking_step_id, "status": "complete"}
                )
            )
            active_thinking_step_id = None
        active_thinking_group["closed"] = True
        active_thinking_group["textEnd"] = visible_text_offset
        active_thinking_group = None
        events.append(ThinkingEndEvent())
        return events

    def persistable_thinking_groups() -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        for group in thinking_groups:
            steps = [
                step
                for step in group.get("steps", [])
                if isinstance(step, dict) and step.get("phase") != "tool"
            ]
            if steps:
                groups.append({**group, "steps": steps})
        return groups

    yield RunStartedEvent(
        type=EventType.RUN_STARTED,
        thread_id=thread_id,
        run_id=run_id,
    )

    try:
        async for event in events:
            event_type = event["type"]
            payload = event["payload"]

            if event_type == "message_start":
                for thinking_event in close_thinking_events():
                    yield thinking_event
                active_text_activity_id = payload["messageId"]
                activity_sequence += 1
                text_activities.append(
                    {
                        "id": active_text_activity_id,
                        "content": "",
                        "status": "streaming",
                        "sequence": activity_sequence,
                    }
                )
                yield TextMessageStartEvent(message_id=payload["messageId"])
            elif event_type == "delta":
                visible_increment = count_visible_text(payload["content"])
                if visible_increment > 0:
                    for thinking_event in close_thinking_events():
                        yield thinking_event
                target_text_id = active_text_activity_id or payload["messageId"]
                for text_activity in text_activities:
                    if text_activity["id"] == target_text_id:
                        text_activity["content"] += payload["content"]
                        break
                else:
                    active_text_activity_id = target_text_id
                    activity_sequence += 1
                    text_activities.append(
                        {
                            "id": target_text_id,
                            "content": payload["content"],
                            "status": "streaming",
                            "sequence": activity_sequence,
                        }
                    )
                yield TextMessageContentEvent(
                    message_id=payload["messageId"],
                    delta=payload["content"],
                )
                visible_text_offset += visible_increment
            elif event_type == "message_end":
                for text_activity in text_activities:
                    if text_activity["id"] == payload["messageId"]:
                        text_activity["status"] = "complete"
                        break
                active_text_activity_id = None
                yield TextMessageEndEvent(message_id=payload["messageId"])
            elif event_type == "tool_start":
                for thinking_event in close_thinking_events():
                    yield thinking_event
                tool_call_id = payload["toolCallId"]
                activity_sequence += 1
                tool_activities.append(
                    {
                        "id": tool_call_id,
                        "name": payload["toolCallName"],
                        "arguments": payload["arguments"],
                        "status": "running",
                        "sequence": activity_sequence,
                        "textOffset": visible_text_offset,
                    }
                )
                yield ToolCallStartEvent(
                    tool_call_id=tool_call_id,
                    tool_call_name=payload["toolCallName"],
                )
                yield ToolCallArgsEvent(
                    tool_call_id=tool_call_id,
                    delta=payload["arguments"],
                )
                yield ToolCallEndEvent(tool_call_id=tool_call_id)
            elif event_type == "tool_result":
                for tool in tool_activities:
                    if tool["id"] == payload["toolCallId"]:
                        tool["result"] = payload["content"]
                        tool["status"] = "complete"
                        break
                yield ToolCallResultEvent(
                    message_id=payload["messageId"],
                    tool_call_id=payload["toolCallId"],
                    content=payload["content"],
                    role="tool",
                )
            elif event_type == "status":
                yield CustomEvent(name="status", value=payload)
            elif event_type == "trace":
                yield CustomEvent(name="trace", value=payload)
            elif event_type == "thinking":
                for thinking_event in thinking_events(payload):
                    yield thinking_event
            elif event_type == "final":
                for thinking_event in close_thinking_events():
                    yield thinking_event
                result = payload
                previous_by_id = {
                    message.id: message
                    for message in previous_messages or []
                }
                snapshot_messages = [
                    message.model_dump(by_alias=True, mode="json")
                    for message in result["messages"]
                ]
                # Agent 会重建上下文消息，这里按消息 ID 继承历史 meta 和时间，
                # 再把本轮 thinking 固化到本轮最终 assistant 消息上。
                for message in snapshot_messages:
                    previous = previous_by_id.get(message["id"])
                    if previous is None:
                        continue
                    message["createdAt"] = previous.created_at.isoformat()
                    if previous.meta is not None:
                        message["meta"] = previous.meta.model_dump(by_alias=True, mode="json")
                for message in reversed(snapshot_messages):
                    if message["role"] != "assistant":
                        continue
                    persisted_thinking_groups = persistable_thinking_groups()
                    message["meta"] = {
                        **(message.get("meta") or {}),
                        "thinkingGroups": persisted_thinking_groups,
                        "toolActivities": tool_activities,
                        "textActivities": text_activities,
                    }
                    break
                yield StateSnapshotEvent(
                    snapshot={
                        "sessionId": thread_id,
                        "messages": snapshot_messages,
                        "trace": result["trace"],
                        "tasks": result["tasks"],
                        "thinking": result["thinking"],
                        "thinkingGroups": persistable_thinking_groups(),
                    }
                )
                yield RunFinishedEvent(
                    thread_id=thread_id,
                    run_id=run_id,
                    result={"sessionId": thread_id},
                )
    except Exception as exc:
        yield RunErrorEvent(message=str(exc), code="AGENT_RUN_ERROR")
