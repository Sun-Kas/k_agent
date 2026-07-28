from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from ag_ui.core import (
    CustomEvent,
    StateSnapshotEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    ThinkingTextMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from access_layer.agui import translate_agent_events
from backend.api.schemas import ChatMessage


class ActivityTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_uses_standard_start_content_end_events(self) -> None:
        assistant = ChatMessage(
            id="assistant-text",
            role="assistant",
            content="hello",
            createdAt=datetime.now(timezone.utc),
        )

        async def events():
            yield {"type": "message_start", "payload": {"messageId": "assistant-text"}}
            yield {
                "type": "delta",
                "payload": {"messageId": "assistant-text", "content": "hello"},
            }
            yield {"type": "message_end", "payload": {"messageId": "assistant-text"}}
            yield {
                "type": "final",
                "payload": {
                    "messages": [assistant],
                    "trace": [],
                    "tasks": [],
                    "thinking": [],
                },
            }

        translated = [
            event
            async for event in translate_agent_events(events(), "thread-1", "run-text")
        ]
        text_events = [
            event for event in translated
            if isinstance(
                event,
                (TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent),
            )
        ]
        self.assertEqual(
            [type(event) for event in text_events],
            [TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent],
        )

    async def test_thinking_ends_before_text_message_starts(self) -> None:
        thinking = {
            "id": "thinking-before-text",
            "phase": "reasoning",
            "title": "准备正文",
            "detail": "ready",
            "status": "complete",
        }
        assistant = ChatMessage(
            id="assistant-after-thinking",
            role="assistant",
            content="answer",
            createdAt=datetime.now(timezone.utc),
        )

        async def events():
            yield {"type": "thinking", "payload": thinking}
            yield {
                "type": "message_start",
                "payload": {"messageId": "assistant-after-thinking"},
            }
            yield {
                "type": "delta",
                "payload": {
                    "messageId": "assistant-after-thinking",
                    "content": "answer",
                },
            }
            yield {
                "type": "message_end",
                "payload": {"messageId": "assistant-after-thinking"},
            }
            yield {
                "type": "final",
                "payload": {
                    "messages": [assistant],
                    "trace": [],
                    "tasks": [],
                    "thinking": [thinking],
                },
            }

        translated = [
            event
            async for event in translate_agent_events(events(), "thread-1", "run-order")
        ]
        thinking_end = next(
            index for index, event in enumerate(translated)
            if isinstance(event, ThinkingEndEvent)
        )
        text_start = next(
            index for index, event in enumerate(translated)
            if isinstance(event, TextMessageStartEvent)
        )
        self.assertLess(thinking_end, text_start)

    async def test_text_activity_order_is_persisted_from_event_order(self) -> None:
        first_thinking = {
            "id": "thinking-first",
            "phase": "reasoning",
            "title": "先思考",
            "detail": "before",
            "status": "complete",
        }
        second_thinking = {
            "id": "thinking-second",
            "phase": "reasoning",
            "title": "后思考",
            "detail": "after",
            "status": "complete",
        }
        assistant = ChatMessage(
            id="assistant-mixed",
            role="assistant",
            content="hello world",
            createdAt=datetime.now(timezone.utc),
        )

        async def events():
            yield {"type": "thinking", "payload": first_thinking}
            yield {"type": "message_start", "payload": {"messageId": "assistant-mixed"}}
            yield {
                "type": "delta",
                "payload": {"messageId": "assistant-mixed", "content": "hello"},
            }
            yield {"type": "thinking", "payload": second_thinking}
            yield {
                "type": "delta",
                "payload": {"messageId": "assistant-mixed", "content": " world"},
            }
            yield {"type": "message_end", "payload": {"messageId": "assistant-mixed"}}
            yield {
                "type": "final",
                "payload": {
                    "messages": [assistant],
                    "trace": [],
                    "tasks": [],
                    "thinking": [first_thinking, second_thinking],
                },
            }

        translated = [
            event
            async for event in translate_agent_events(events(), "thread-1", "run-mixed")
        ]
        snapshot_event = next(
            event for event in translated
            if isinstance(event, StateSnapshotEvent)
        )
        meta = snapshot_event.snapshot["messages"][0]["meta"]
        text_sequence = meta["textActivities"][0]["sequence"]
        thinking_sequences = [
            group["sequence"] for group in meta["thinkingGroups"]
        ]
        self.assertLess(thinking_sequences[0], text_sequence)
        self.assertLess(text_sequence, thinking_sequences[1])
        self.assertEqual(meta["textActivities"][0]["content"], "hello world")

    async def test_tool_is_a_hard_boundary_between_thinking_groups(self) -> None:
        first = {
            "id": "thinking-1",
            "phase": "reasoning",
            "title": "第一次思考",
            "detail": "before tool",
            "status": "complete",
        }
        tool_step = {
            "id": "tool-step",
            "phase": "tool",
            "title": "调用 search",
            "detail": "searching",
            "status": "complete",
        }
        second = {
            "id": "thinking-2",
            "phase": "reasoning",
            "title": "新的思考",
            "detail": "after tool",
            "status": "complete",
        }
        assistant = ChatMessage(
            id="assistant-1",
            role="assistant",
            content="done",
            createdAt=datetime.now(timezone.utc),
        )

        async def events():
            yield {"type": "thinking", "payload": first}
            yield {"type": "thinking", "payload": tool_step}
            yield {
                "type": "tool_start",
                "payload": {
                    "toolCallId": "call-1",
                    "toolCallName": "search",
                    "arguments": '{"q":"test"}',
                },
            }
            yield {
                "type": "tool_result",
                "payload": {
                    "toolCallId": "call-1",
                    "messageId": "tool-message-1",
                    "content": "result",
                },
            }
            yield {"type": "thinking", "payload": second}
            yield {
                "type": "final",
                "payload": {
                    "messages": [assistant],
                    "trace": [],
                    "tasks": [],
                    "thinking": [first, tool_step, second],
                },
            }

        translated: list[Any] = [
            event
            async for event in translate_agent_events(events(), "thread-1", "run-1")
        ]
        thinking_starts = [
            index for index, event in enumerate(translated)
            if isinstance(event, ThinkingStartEvent)
        ]
        thinking_ends = [
            index for index, event in enumerate(translated)
            if isinstance(event, ThinkingEndEvent)
        ]
        first_tool_start = next(
            index for index, event in enumerate(translated)
            if isinstance(event, ToolCallStartEvent)
        )
        first_tool_end = next(
            index for index, event in enumerate(translated)
            if isinstance(event, ToolCallEndEvent)
        )
        first_tool_result = next(
            index for index, event in enumerate(translated)
            if isinstance(event, ToolCallResultEvent)
        )
        self.assertEqual(len(thinking_starts), 2)
        self.assertEqual(len(thinking_ends), 2)
        self.assertLess(thinking_starts[0], thinking_ends[0])
        self.assertLess(thinking_ends[0], first_tool_start)
        self.assertLess(first_tool_start, first_tool_end)
        self.assertLess(first_tool_end, first_tool_result)
        self.assertLess(first_tool_result, thinking_starts[1])
        self.assertLess(thinking_starts[1], thinking_ends[1])
        self.assertTrue(any(isinstance(event, ThinkingTextMessageStartEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ThinkingTextMessageContentEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ThinkingTextMessageEndEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ToolCallEndEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ToolCallResultEvent) for event in translated))
        self.assertFalse(
            any(
                isinstance(event, CustomEvent) and event.name == "thinking"
                for event in translated
            )
        )
        snapshot_event = next(
            event for event in translated if isinstance(event, StateSnapshotEvent)
        )
        snapshot = snapshot_event.snapshot
        groups = snapshot["thinkingGroups"]
        self.assertEqual([[step["id"] for step in group["steps"]] for group in groups], [["thinking-1"], ["thinking-2"]])
        self.assertLess(groups[0]["sequence"], snapshot["messages"][0]["meta"]["toolActivities"][0]["sequence"])
        self.assertLess(snapshot["messages"][0]["meta"]["toolActivities"][0]["sequence"], groups[1]["sequence"])
        self.assertEqual(snapshot["messages"][0]["meta"]["toolActivities"][0]["result"], "result")


if __name__ == "__main__":
    unittest.main()
