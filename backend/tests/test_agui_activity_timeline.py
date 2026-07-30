from __future__ import annotations

import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from typing import Any

from ag_ui.core import (
    CustomEvent,
    ReasoningEndEvent,
    ReasoningStartEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from backend.agui import to_chat_messages, translate_agent_events
from access_layer.sessions.store import SessionStore
from backend.api.schemas import ChatMessage
from backend.storage import FileStorage


class ActivityTimelineTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_assistant_placeholder_is_not_input_history(self) -> None:
        messages = [
            {"id": "user-1", "role": "user", "content": "hello"},
            {"id": "pending", "role": "assistant", "content": ""},
        ]

        converted = to_chat_messages(messages)

        self.assertEqual([message.id for message in converted], ["user-1"])

    async def test_session_accumulates_text_before_saving_complete_message(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-store")
        user = ChatMessage(
            id="user-1",
            role="user",
            content="hello",
            createdAt=datetime.now(timezone.utc),
        )
        await store.save_run_start(
            "thread-store",
            [user],
            mcp_server_ids=[],
            skill_ids=[],
        )
        await store.append_event(
            "thread-store",
            {"type": "RUN_STARTED", "threadId": "thread-store", "runId": "run-1"},
        )
        await store.append_event(
            "thread-store",
            {"type": "TEXT_MESSAGE_START", "messageId": "assistant-1"},
        )
        await store.append_event(
            "thread-store",
            {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": "assistant-1",
                "delta": "hel",
            },
        )

        session = await store.get("thread-store")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual([message.id for message in session.messages], ["user-1"])

        await store.append_event(
            "thread-store",
            {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": "assistant-1",
                "delta": "lo",
            },
        )
        await store.append_event(
            "thread-store",
            {"type": "TEXT_MESSAGE_END", "messageId": "assistant-1"},
        )

        session = await store.get("thread-store")
        assert session is not None
        self.assertEqual(
            [(message.id, message.content) for message in session.messages],
            [("user-1", "hello"), ("assistant-1", "hello")],
        )
        self.assertEqual(session.messages[-1].meta.run_id, "run-1")

    async def test_new_run_preserves_prior_messages_and_events(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-history")
        first_user = ChatMessage(
            id="user-1",
            role="user",
            content="first",
            createdAt=datetime.now(timezone.utc),
        )
        first_assistant = ChatMessage(
            id="assistant-1",
            role="assistant",
            content="answer",
            createdAt=datetime.now(timezone.utc),
        )
        await store.save_run_start(
            "thread-history",
            [first_user, first_assistant],
            mcp_server_ids=[],
            skill_ids=[],
        )
        await store.append_event(
            "thread-history",
            {"type": "RUN_STARTED", "threadId": "thread-history", "runId": "run-1"},
        )
        second_user = ChatMessage(
            id="user-2",
            role="user",
            content="second",
            createdAt=datetime.now(timezone.utc),
        )

        await store.save_run_start(
            "thread-history",
            [first_user, second_user],
            mcp_server_ids=[],
            skill_ids=[],
        )

        session = await store.get("thread-history")
        assert session is not None
        self.assertEqual(
            [message.id for message in session.messages],
            ["user-1", "assistant-1", "user-2"],
        )
        self.assertEqual([event["type"] for event in session.events], ["RUN_STARTED"])

    async def test_session_capability_selection_survives_reload_and_empty_selection(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = FileStorage(tmp)
            store = SessionStore(storage)
            await store.create_session(session_id="thread-capabilities")
            user = ChatMessage(
                id="user-capabilities",
                role="user",
                content="use selected capabilities",
                createdAt=datetime.now(timezone.utc),
            )
            await store.save_run_start(
                "thread-capabilities",
                [user],
                mcp_server_ids=["mcp-a", "mcp-a"],
                skill_ids=["skill-a"],
            )

            reloaded = await SessionStore(storage).get("thread-capabilities")
            assert reloaded is not None
            self.assertEqual(reloaded.mcp_server_ids, ["mcp-a"])
            self.assertEqual(reloaded.skill_ids, ["skill-a"])

            await store.save_run_start(
                "thread-capabilities",
                [],
                mcp_server_ids=[],
                skill_ids=[],
            )
            cleared = await SessionStore(storage).get("thread-capabilities")
            assert cleared is not None
            self.assertEqual(cleared.mcp_server_ids, [])
            self.assertEqual(cleared.skill_ids, [])

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
            if isinstance(event, ReasoningEndEvent)
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
        event_types = [event.type for event in translated]
        first_thinking_start = event_types.index("REASONING_START")
        text_start = event_types.index("TEXT_MESSAGE_START")
        second_thinking_start = event_types.index("REASONING_START", first_thinking_start + 1)
        self.assertLess(first_thinking_start, text_start)
        self.assertLess(text_start, second_thinking_start)

    async def test_tool_is_a_hard_boundary_between_thinking_blocks(self) -> None:
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
            if isinstance(event, ReasoningStartEvent)
        ]
        thinking_ends = [
            index for index, event in enumerate(translated)
            if isinstance(event, ReasoningEndEvent)
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
        self.assertTrue(any(isinstance(event, ReasoningMessageStartEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ReasoningMessageContentEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ReasoningMessageEndEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ToolCallEndEvent) for event in translated))
        self.assertTrue(any(isinstance(event, ToolCallResultEvent) for event in translated))
        self.assertFalse(
            any(
                isinstance(event, CustomEvent) and event.name == "thinking"
                for event in translated
            )
        )
        event_types = [event.type for event in translated]
        persisted_first_thinking_start = event_types.index("REASONING_START")
        persisted_first_tool_start = event_types.index("TOOL_CALL_START")
        persisted_tool_result = event_types.index("TOOL_CALL_RESULT")
        persisted_second_thinking_start = event_types.index("REASONING_START", persisted_first_thinking_start + 1)
        self.assertLess(persisted_first_thinking_start, persisted_first_tool_start)
        self.assertLess(persisted_first_tool_start, persisted_tool_result)
        self.assertLess(persisted_tool_result, persisted_second_thinking_start)

    async def test_duplicate_completed_thinking_after_text_is_ignored(self) -> None:
        thinking = {
            "id": "thinking-repeat",
            "phase": "reasoning",
            "title": "分析并决定下一步",
            "detail": "done",
            "status": "complete",
        }
        assistant = ChatMessage(
            id="assistant-repeat",
            role="assistant",
            content="answer",
            createdAt=datetime.now(timezone.utc),
        )

        async def events():
            yield {"type": "thinking", "payload": thinking}
            yield {"type": "message_start", "payload": {"messageId": "assistant-repeat"}}
            yield {"type": "delta", "payload": {"messageId": "assistant-repeat", "content": "answer"}}
            yield {"type": "message_end", "payload": {"messageId": "assistant-repeat"}}
            yield {"type": "thinking", "payload": thinking}
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
            async for event in translate_agent_events(events(), "thread-1", "run-repeat")
        ]

        self.assertEqual(
            [event.type for event in translated].count("REASONING_START"),
            1,
        )
        self.assertEqual(
            [event.type for event in translated].count("REASONING_END"),
            1,
        )

    async def test_active_thinking_closed_by_text_is_not_reopened_by_late_completion(self) -> None:
        active_thinking = {
            "id": "thinking-active-before-text",
            "phase": "reasoning",
            "title": "分析并决定下一步",
            "detail": "working",
            "status": "active",
        }
        complete_thinking = {
            **active_thinking,
            "detail": "done",
            "status": "complete",
        }

        async def events():
            yield {"type": "thinking", "payload": active_thinking}
            yield {"type": "message_start", "payload": {"messageId": "assistant-late"}}
            yield {
                "type": "delta",
                "payload": {"messageId": "assistant-late", "content": "answer"},
            }
            yield {"type": "message_end", "payload": {"messageId": "assistant-late"}}
            yield {"type": "thinking", "payload": complete_thinking}
            yield {"type": "final", "payload": {"messages": [], "trace": [], "tasks": []}}

        translated = [
            event
            async for event in translate_agent_events(events(), "thread-1", "run-late")
        ]
        event_types = [event.type for event in translated]

        self.assertEqual(event_types.count("REASONING_START"), 1)
        self.assertEqual(event_types.count("REASONING_END"), 1)
        self.assertLess(
            event_types.index("REASONING_END"),
            event_types.index("TEXT_MESSAGE_START"),
        )

    async def test_error_closes_open_thinking_before_run_error(self) -> None:
        thinking = {
            "id": "thinking-error",
            "phase": "reasoning",
            "title": "分析",
            "detail": "working",
            "status": "active",
        }

        async def events():
            yield {"type": "thinking", "payload": thinking}
            raise RuntimeError("boom")

        translated = [
            event
            async for event in translate_agent_events(events(), "thread-1", "run-error")
        ]
        event_types = [event.type for event in translated]

        self.assertLess(event_types.index("REASONING_END"), event_types.index("RUN_ERROR"))

if __name__ == "__main__":
    unittest.main()
