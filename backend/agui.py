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
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from backend.schemas import ChatMessage


encoder = EventEncoder()


def encode_event(event: Any) -> str:
    return encoder.encode(event)


def to_chat_messages(messages: list[Any]) -> list[ChatMessage]:
    converted: list[ChatMessage] = []
    for message in messages:
        role = getattr(message, "role", None)
        content = getattr(message, "content", "")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        converted.append(
            ChatMessage(
                id=getattr(message, "id", None) or str(uuid.uuid4()),
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
) -> AsyncIterator[Any]:
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
                yield TextMessageStartEvent(message_id=payload["messageId"])
            elif event_type == "delta":
                yield TextMessageContentEvent(
                    message_id=payload["messageId"],
                    delta=payload["content"],
                )
            elif event_type == "message_end":
                yield TextMessageEndEvent(message_id=payload["messageId"])
            elif event_type == "tool_start":
                tool_call_id = payload["toolCallId"]
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
                yield CustomEvent(name="thinking", value=payload)
            elif event_type == "final":
                result = payload
                yield StateSnapshotEvent(
                    snapshot={
                        "sessionId": thread_id,
                        "messages": [
                            message.model_dump(by_alias=True, mode="json")
                            for message in result["messages"]
                        ],
                        "trace": result["trace"],
                        "tasks": result["tasks"],
                        "thinking": result["thinking"],
                    }
                )
                yield RunFinishedEvent(
                    thread_id=thread_id,
                    run_id=run_id,
                    result={"sessionId": thread_id},
                )
    except Exception as exc:
        yield RunErrorEvent(message=str(exc), code="AGENT_RUN_ERROR")
