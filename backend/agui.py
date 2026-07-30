"""Create and encode the standard AG-UI event stream emitted by Agent Backend."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ag_ui.core import (
    CustomEvent,
    EventType,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from backend.api.schemas import ChatMessage


encoder = EventEncoder()


def encode_event(event: Any) -> str:
    """把 AG-UI event 模型编码为传输文本。"""
    return encoder.encode(event)


def to_chat_messages(messages: list[Any]) -> list[ChatMessage]:
    """把 AG-UI 输入消息转换为后端 ChatMessage 并过滤空 assistant。"""
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
        if role == "assistant" and not content.strip():
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
    """把 Agent 内部事件按流式顺序转换为标准 AG-UI events。"""
    agui_events: list[dict[str, Any]] = []
    reasoning_message_id: str | None = None
    active_reasoning_message_id: str | None = None
    active_reasoning_step: dict[str, Any] | None = None
    completed_reasoning_message_ids: set[str] = set()

    def reasoning_events(step: dict[str, Any]) -> list[Any]:
        """把内部 thinking step 转换为 AG-UI reasoning start/content/end 事件。"""
        nonlocal reasoning_message_id, active_reasoning_message_id, active_reasoning_step
        if step.get("phase") == "tool":
            return []

        # thinking 在 AG-UI 中映射为 REASONING：同一个 step 只增量追加；
        # 一旦 step 完成，就发 REASONING_MESSAGE_END，后续同 ID 的迟到完成态不再重开。
        events: list[Any] = []
        step_id = str(step.get("id") or uuid.uuid4())
        detail = str(step.get("detail") or "")
        if (
            step.get("status") != "active"
            and step_id in completed_reasoning_message_ids
            and active_reasoning_message_id != step_id
        ):
            return []
        if reasoning_message_id is None:
            reasoning_message_id = str(uuid.uuid4())
            events.append(ReasoningStartEvent(message_id=reasoning_message_id))
        if active_reasoning_message_id == step_id and active_reasoning_step is not None:
            previous_detail = str(active_reasoning_step.get("detail") or "")
            active_reasoning_step = {**step, "id": step_id}
            if detail.startswith(previous_detail):
                delta = detail[len(previous_detail):]
                if delta:
                    events.append(
                        ReasoningMessageContentEvent(
                            message_id=step_id,
                            delta=delta,
                            raw_event={"id": step_id},
                        )
                    )
            else:
                events.append(
                    ReasoningMessageEndEvent(
                        message_id=step_id,
                        raw_event={**active_reasoning_step, "status": "complete"}
                    )
                )
                events.append(ReasoningMessageStartEvent(
                    message_id=step_id,
                    role="reasoning",
                    raw_event=active_reasoning_step,
                ))
                if detail:
                    events.append(
                        ReasoningMessageContentEvent(
                            message_id=step_id,
                            delta=detail,
                            raw_event={"id": step_id},
                        )
                    )
            if step.get("status") != "active":
                events.append(ReasoningMessageEndEvent(
                    message_id=step_id,
                    raw_event=active_reasoning_step,
                ))
                completed_reasoning_message_ids.add(step_id)
                active_reasoning_message_id = None
                active_reasoning_step = None
            return events

        if active_reasoning_message_id is not None:
            completed_reasoning_message_ids.add(active_reasoning_message_id)
            events.append(
                ReasoningMessageEndEvent(
                    message_id=active_reasoning_message_id,
                    raw_event={"id": active_reasoning_message_id, "status": "complete"}
                )
            )
        active_reasoning_message_id = step_id
        normalized_step = {**step, "id": step_id}
        active_reasoning_step = normalized_step
        events.append(ReasoningMessageStartEvent(
            message_id=step_id,
            role="reasoning",
            raw_event=normalized_step,
        ))
        if detail:
            events.append(
                ReasoningMessageContentEvent(
                    message_id=step_id,
                    delta=detail,
                    raw_event={"id": active_reasoning_message_id},
                )
            )
        if normalized_step.get("status") != "active":
            events.append(ReasoningMessageEndEvent(
                message_id=step_id,
                raw_event=normalized_step,
            ))
            completed_reasoning_message_ids.add(step_id)
            active_reasoning_message_id = None
            active_reasoning_step = None
        return events

    def close_reasoning_events() -> list[Any]:
        """在正文、工具或结束边界关闭当前 reasoning 块。"""
        nonlocal reasoning_message_id, active_reasoning_message_id, active_reasoning_step
        if reasoning_message_id is None:
            return []
        events: list[Any] = []
        # 正文、工具、结束事件都是 reasoning 的硬边界。前端看到 end 后
        # 就应该关闭当前 thinking 块；如果后面还有 thinking，会重新 start。
        if active_reasoning_message_id is not None:
            completed_reasoning_message_ids.add(active_reasoning_message_id)
            events.append(
                ReasoningMessageEndEvent(
                    message_id=active_reasoning_message_id,
                    raw_event={"id": active_reasoning_message_id, "status": "complete"}
                )
            )
            active_reasoning_message_id = None
            active_reasoning_step = None
        events.append(ReasoningEndEvent(message_id=reasoning_message_id))
        reasoning_message_id = None
        return events

    def event_payload(event: Any) -> dict[str, Any]:
        """把 AG-UI 事件模型转为可持久化字典。"""
        if hasattr(event, "model_dump"):
            return event.model_dump(by_alias=True, mode="json", exclude_none=True)
        return {"type": getattr(event, "type", type(event).__name__)}

    def remember(event: Any) -> Any:
        """记录即将输出的 AG-UI event 并返回原事件。"""
        agui_events.append(event_payload(event))
        return event

    yield remember(RunStartedEvent(
        type=EventType.RUN_STARTED,
        thread_id=thread_id,
        run_id=run_id,
    ))

    try:
        async for event in events:
            event_type = event["type"]
            payload = event["payload"]

            if event_type == "message_start":
                for reasoning_event in close_reasoning_events():
                    yield remember(reasoning_event)
                yield remember(TextMessageStartEvent(message_id=payload["messageId"]))
            elif event_type == "delta":
                if str(payload["content"]).strip():
                    for reasoning_event in close_reasoning_events():
                        yield remember(reasoning_event)
                yield remember(TextMessageContentEvent(
                    message_id=payload["messageId"],
                    delta=payload["content"],
                ))
            elif event_type == "message_end":
                yield remember(TextMessageEndEvent(message_id=payload["messageId"]))
            elif event_type == "tool_start":
                for reasoning_event in close_reasoning_events():
                    yield remember(reasoning_event)
                tool_call_id = payload["toolCallId"]
                yield remember(ToolCallStartEvent(
                    tool_call_id=tool_call_id,
                    tool_call_name=payload["toolCallName"],
                ))
                yield remember(ToolCallArgsEvent(
                    tool_call_id=tool_call_id,
                    delta=payload["arguments"],
                ))
                yield remember(ToolCallEndEvent(tool_call_id=tool_call_id))
            elif event_type == "tool_result":
                yield remember(ToolCallResultEvent(
                    message_id=payload["messageId"],
                    tool_call_id=payload["toolCallId"],
                    content=payload["content"],
                    role="tool",
                ))
            elif event_type == "status":
                yield remember(CustomEvent(name="status", value=payload))
            elif event_type == "trace":
                yield remember(CustomEvent(name="trace", value=payload))
            elif event_type == "thinking":
                for reasoning_event in reasoning_events(payload):
                    yield remember(reasoning_event)
            elif event_type == "final":
                for reasoning_event in close_reasoning_events():
                    yield remember(reasoning_event)
                yield remember(RunFinishedEvent(
                    thread_id=thread_id,
                    run_id=run_id,
                    result={"sessionId": thread_id},
                ))
    except Exception as exc:
        for reasoning_event in close_reasoning_events():
            yield remember(reasoning_event)
        yield remember(RunErrorEvent(message=str(exc), code="AGENT_RUN_ERROR"))
