from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import unittest

from access_layer.sessions.store import SessionStore
from backend.api.schemas import ChatMessage, ChatMeta, ToolCallRecord
from backend.context import (
    compose_api_messages,
    group_api_rounds,
    pair_tool_messages,
)


def message(
    identifier: str,
    role: str,
    content: str = "",
    *,
    tool_calls: list[ToolCallRecord] | None = None,
    tool_call_id: str | None = None,
) -> ChatMessage:
    return ChatMessage(
        id=identifier,
        role=role,
        content=content,
        createdAt=datetime.now(timezone.utc),
        meta=ChatMeta(toolCallId=tool_call_id) if tool_call_id else None,
        toolCalls=tool_calls or [],
    )


def tool_turn(call_id: str, name: str = "Read", output: str = "file body") -> list[ChatMessage]:
    return [
        message(
            f"toolcall-{call_id}",
            "assistant",
            tool_calls=[ToolCallRecord(id=call_id, name=name, arguments="{}")],
        ),
        message(f"toolresult-{call_id}", "tool", output, tool_call_id=call_id),
    ]


class ToolHistoryProjectionTests(unittest.TestCase):
    """AG-UI tool events must survive into the next run's model input."""

    def setUp(self) -> None:
        self.store = SessionStore(storage=None)
        asyncio.run(self.store.create_session(session_id="s-1"))

    def feed(self, events: list[dict]) -> list[ChatMessage]:
        async def run() -> list[ChatMessage]:
            record = None
            for event in events:
                record = await self.store.append_event("s-1", event)
            return record.messages if record else []

        return asyncio.run(run())

    def test_completed_tool_call_persists_as_assistant_and_tool_pair(self) -> None:
        messages = self.feed([
            {"type": "RUN_STARTED", "runId": "r-1"},
            {"type": "TOOL_CALL_START", "toolCallId": "c-1", "toolCallName": "Read"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "c-1", "delta": '{"file_path"'},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "c-1", "delta": ': "a.txt"}'},
            {"type": "TOOL_CALL_END", "toolCallId": "c-1"},
            {
                "type": "TOOL_CALL_RESULT",
                "toolCallId": "c-1",
                "messageId": "m-tool-1",
                "content": "hello",
            },
            {"type": "RUN_FINISHED", "runId": "r-1"},
        ])
        self.assertEqual([item.role for item in messages], ["assistant", "tool"])
        self.assertEqual(messages[0].tool_calls[0].name, "Read")
        self.assertEqual(messages[0].tool_calls[0].arguments, '{"file_path": "a.txt"}')
        self.assertEqual(messages[1].content, "hello")
        self.assertEqual(messages[1].meta.tool_call_id, "c-1")

    def test_tool_call_without_result_is_not_persisted(self) -> None:
        messages = self.feed([
            {"type": "RUN_STARTED", "runId": "r-1"},
            {"type": "TOOL_CALL_START", "toolCallId": "c-1", "toolCallName": "Bash"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "c-1", "delta": "{}"},
            {"type": "RUN_ERROR", "message": "boom"},
        ])
        self.assertEqual(messages, [])


class ToolPairingTests(unittest.TestCase):
    def test_orphan_tool_result_is_dropped(self) -> None:
        messages = [message("t-1", "tool", "result", tool_call_id="c-1")]
        self.assertEqual(pair_tool_messages(messages), [])

    def test_unanswered_tool_call_is_dropped(self) -> None:
        messages = [
            message(
                "a-1",
                "assistant",
                tool_calls=[ToolCallRecord(id="c-1", name="Read")],
            )
        ]
        self.assertEqual(pair_tool_messages(messages), [])

    def test_assistant_text_survives_when_its_call_is_unanswered(self) -> None:
        messages = [
            message(
                "a-1",
                "assistant",
                "let me check",
                tool_calls=[ToolCallRecord(id="c-1", name="Read")],
            )
        ]
        repaired = pair_tool_messages(messages)
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0].tool_calls, [])

    def test_provider_payload_replays_tool_calls(self) -> None:
        body = compose_api_messages(
            [message("u-1", "user", "read a.txt"), *tool_turn("c-1")],
            system_prompt="system",
            user_context={},
        )
        assistant = next(item for item in body if item.get("tool_calls"))
        self.assertEqual(assistant["tool_calls"][0]["id"], "c-1")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "Read")
        tool_message = next(item for item in body if item["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "c-1")

    def test_compaction_never_orphans_a_tool_result(self) -> None:
        messages: list[ChatMessage] = []
        for index in range(6):
            messages.append(message(f"u-{index}", "user", "ask " + "x" * 4000))
            messages.extend(tool_turn(f"c-{index}", output="y" * 4000))
        provider = compose_api_messages(messages, system_prompt="system", user_context={})
        groups = group_api_rounds([item for item in provider if item["role"] != "system"])
        for group in groups:
            announced = {
                str(call.get("id"))
                for item in group
                for call in item.get("tool_calls") or []
            }
            for item in group:
                if item["role"] == "tool":
                    self.assertIn(item["tool_call_id"], announced)


if __name__ == "__main__":
    unittest.main()
