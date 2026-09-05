from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from access_layer.sessions.durable_events import coalesce_durable_events
from access_layer.sessions.store import SessionStore
from backend.api.schemas import ChatMessage
from backend.storage import FileStorage


def user_message(message_id: str = "user-1", content: str = "hello") -> ChatMessage:
    return ChatMessage(
        id=message_id,
        role="user",
        content=content,
        createdAt=datetime.now(timezone.utc),
    )


class DurableEventCoalesceTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_persists_one_content_event_per_text_block(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-coalesce")
        await store.save_run_start(
            "thread-coalesce", [user_message()], mcp_server_ids=[], skill_ids=[]
        )
        for event in (
            {"type": "RUN_STARTED", "threadId": "thread-coalesce", "runId": "run-1"},
            {"type": "TEXT_MESSAGE_START", "messageId": "assistant-1"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant-1", "delta": "hel"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant-1", "delta": "lo "},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant-1", "delta": "world"},
            {"type": "TEXT_MESSAGE_END", "messageId": "assistant-1"},
            {"type": "RUN_FINISHED", "threadId": "thread-coalesce", "runId": "run-1"},
        ):
            await store.append_event("thread-coalesce", event)

        session = await store.get("thread-coalesce")
        assert session is not None
        content_events = [
            event for event in session.events if event.get("type") == "TEXT_MESSAGE_CONTENT"
        ]
        self.assertEqual(len(content_events), 1)
        self.assertEqual(content_events[0]["delta"], "hello world")
        self.assertEqual(
            [event["type"] for event in session.events],
            [
                "input_message",
                "RUN_STARTED",
                "TEXT_MESSAGE_START",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_END",
                "RUN_FINISHED",
            ],
        )
        self.assertEqual(session.messages[-1].content, "hello world")

    async def test_deltas_are_not_stored_before_the_block_closes(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-pending")
        await store.save_run_start(
            "thread-pending", [user_message()], mcp_server_ids=[], skill_ids=[]
        )
        await store.append_event(
            "thread-pending",
            {"type": "RUN_STARTED", "threadId": "thread-pending", "runId": "run-1"},
        )
        await store.append_event(
            "thread-pending",
            {"type": "TEXT_MESSAGE_START", "messageId": "assistant-1"},
        )
        await store.append_event(
            "thread-pending",
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant-1", "delta": "partial"},
        )
        session = await store.get("thread-pending")
        assert session is not None
        self.assertEqual(
            [event["type"] for event in session.events],
            ["input_message", "RUN_STARTED", "TEXT_MESSAGE_START"],
        )

    async def test_tool_args_and_reasoning_are_stored_as_full_blocks(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-tools")
        await store.save_run_start(
            "thread-tools", [user_message()], mcp_server_ids=[], skill_ids=[]
        )
        for event in (
            {"type": "RUN_STARTED", "threadId": "thread-tools", "runId": "run-1"},
            {"type": "REASONING_START", "messageId": "think-1"},
            {"type": "REASONING_MESSAGE_START", "messageId": "step-1"},
            {"type": "REASONING_MESSAGE_CONTENT", "messageId": "step-1", "delta": "先"},
            {"type": "REASONING_MESSAGE_CONTENT", "messageId": "step-1", "delta": "看"},
            {"type": "REASONING_MESSAGE_END", "messageId": "step-1"},
            {"type": "REASONING_END", "messageId": "think-1"},
            {"type": "TOOL_CALL_START", "toolCallId": "call-1", "toolCallName": "Read"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": '{"file'},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": '_path":"a.txt"}'},
            {"type": "TOOL_CALL_END", "toolCallId": "call-1"},
            {
                "type": "TOOL_CALL_RESULT",
                "toolCallId": "call-1",
                "messageId": "result-1",
                "content": "ok",
            },
            {"type": "RUN_FINISHED", "threadId": "thread-tools", "runId": "run-1"},
        ):
            await store.append_event("thread-tools", event)

        session = await store.get("thread-tools")
        assert session is not None
        self.assertEqual(
            [
                (event["type"], event.get("delta") or event.get("toolCallId") or event.get("messageId"))
                for event in session.events
            ],
            [
                ("input_message", None),
                ("RUN_STARTED", None),
                ("REASONING_START", "think-1"),
                ("REASONING_MESSAGE_START", "step-1"),
                ("REASONING_MESSAGE_CONTENT", "先看"),
                ("REASONING_MESSAGE_END", "step-1"),
                ("REASONING_END", "think-1"),
                ("TOOL_CALL_START", "call-1"),
                ("TOOL_CALL_ARGS", '{"file_path":"a.txt"}'),
                ("TOOL_CALL_END", "call-1"),
                ("TOOL_CALL_RESULT", "call-1"),
                ("RUN_FINISHED", None),
            ],
        )

    async def test_omitted_reasoning_end_still_lands_before_the_tool(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-order")
        await store.save_run_start(
            "thread-order", [user_message()], mcp_server_ids=[], skill_ids=[]
        )
        for event in (
            {"type": "RUN_STARTED", "threadId": "thread-order", "runId": "run-1"},
            {"type": "REASONING_START", "messageId": "think-1"},
            {"type": "REASONING_MESSAGE_CONTENT", "messageId": "think-1", "delta": "准备调用。"},
            {"type": "TOOL_CALL_START", "toolCallId": "call-1", "toolCallName": "Bash"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": "{}"},
            {"type": "TOOL_CALL_END", "toolCallId": "call-1"},
            {"type": "RUN_FINISHED", "threadId": "thread-order", "runId": "run-1"},
        ):
            await store.append_event("thread-order", event)

        session = await store.get("thread-order")
        assert session is not None
        types = [event["type"] for event in session.events]
        self.assertLess(
            types.index("REASONING_MESSAGE_CONTENT"),
            types.index("TOOL_CALL_START"),
        )
        reasoning = next(
            event for event in session.events if event["type"] == "REASONING_MESSAGE_CONTENT"
        )
        self.assertEqual(reasoning["delta"], "准备调用。")

    async def test_stop_run_writes_accumulated_partial_content(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-stop")
        await store.save_run_start(
            "thread-stop",
            [user_message("user-stop", "keep")],
            run_id="run-stop",
            mcp_server_ids=[],
            skill_ids=[],
        )
        await store.append_event(
            "thread-stop",
            {"type": "RUN_STARTED", "threadId": "thread-stop", "runId": "run-stop"},
        )
        await store.append_event(
            "thread-stop",
            {"type": "TEXT_MESSAGE_START", "messageId": "assistant-stop"},
        )
        await store.append_event(
            "thread-stop",
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant-stop", "delta": "par"},
        )
        await store.append_event(
            "thread-stop",
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant-stop", "delta": "tial"},
        )
        stopped = await store.stop_run("thread-stop", "run-stop")
        assert stopped is not None
        content_events = [
            event for event in stopped.events if event.get("type") == "TEXT_MESSAGE_CONTENT"
        ]
        self.assertEqual(content_events, [{
            "type": "TEXT_MESSAGE_CONTENT",
            "messageId": "assistant-stop",
            "delta": "partial",
        }])
        self.assertEqual(stopped.events[-1]["result"]["status"], "stopped")

    async def test_run_error_seals_partial_text_before_terminal_event(self) -> None:
        store = SessionStore()
        await store.create_session(session_id="thread-error")
        await store.save_run_start(
            "thread-error",
            [user_message("user-error", "keep")],
            run_id="run-error",
            mcp_server_ids=[],
            skill_ids=[],
        )
        for event in (
            {"type": "RUN_STARTED", "threadId": "thread-error", "runId": "run-error"},
            {"type": "TEXT_MESSAGE_START", "messageId": "assistant-error"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant-error", "delta": "partial"},
            {"type": "RUN_ERROR", "runId": "run-error", "message": "failed"},
        ):
            await store.append_event("thread-error", event)
        session = await store.get("thread-error")
        assert session is not None
        self.assertEqual(
            [event["type"] for event in session.events[-3:]],
            ["TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_ERROR"],
        )
        self.assertEqual(session.messages[-1].content, "partial")

    async def test_loading_rewrites_legacy_token_deltas(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = FileStorage(tmp)
            writer = SessionStore(storage)
            await writer.create_session(session_id="legacy")
            session = await writer.get("legacy")
            assert session is not None
            session.events = [
                {"type": "RUN_STARTED", "threadId": "legacy", "runId": "run-1"},
                {"type": "TEXT_MESSAGE_START", "messageId": "a1"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "你"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "好"},
                {"type": "TEXT_MESSAGE_END", "messageId": "a1"},
                {
                    "type": "CUSTOM",
                    "name": "tool_output_delta",
                    "value": {"toolCallId": "c1", "delta": "out"},
                },
                {"type": "RUN_FINISHED", "threadId": "legacy", "runId": "run-1"},
            ]
            await writer.update("legacy", [], [], [])
            # update() coalesces on assign; write the raw token stream through storage.
            await storage.write_json("sessions/legacy/legacy.json", {
                **writer._record_to_payload(session),
                "events": [
                    {"type": "RUN_STARTED", "threadId": "legacy", "runId": "run-1"},
                    {"type": "TEXT_MESSAGE_START", "messageId": "a1"},
                    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "你"},
                    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "好"},
                    {"type": "TEXT_MESSAGE_END", "messageId": "a1"},
                    {
                        "type": "CUSTOM",
                        "name": "tool_output_delta",
                        "value": {"toolCallId": "c1", "delta": "out"},
                    },
                    {"type": "RUN_FINISHED", "threadId": "legacy", "runId": "run-1"},
                ],
            })

            reloaded = SessionStore(storage)
            loaded = await reloaded.get("legacy")
            assert loaded is not None
            self.assertEqual(
                [event.get("delta") for event in loaded.events if event["type"] == "TEXT_MESSAGE_CONTENT"],
                ["你好"],
            )
            self.assertFalse(
                any(event.get("name") == "tool_output_delta" for event in loaded.events)
            )
            persisted = await storage.read_json("sessions/legacy/session.json")
            self.assertNotIn("events", persisted)
            history = await storage.read_text_range("sessions/legacy/history.jsonl")
            event_types = [
                event["type"]
                for line in history
                for event in json.loads(line).get("events", [])
            ]
            self.assertEqual(event_types, [
                "RUN_STARTED",
                "TEXT_MESSAGE_START",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_END",
                "RUN_FINISHED",
            ])
            self.assertTrue(storage.resolve("sessions/legacy/legacy.json.bak").is_file())


class CoalesceFunctionTests(unittest.TestCase):
    def test_adjacent_deltas_collapse_and_stdout_deltas_are_dropped(self) -> None:
        coalesced = coalesce_durable_events([
            {"type": "RUN_STARTED", "runId": "r1"},
            {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "A"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "B"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "TOOL_CALL_START", "toolCallId": "t1", "toolCallName": "Bash"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "t1", "delta": "{"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "t1", "delta": "}"},
            {
                "type": "CUSTOM",
                "name": "tool_output_delta",
                "value": {"toolCallId": "t1", "delta": "x"},
            },
            {"type": "TOOL_CALL_END", "toolCallId": "t1"},
        ])
        self.assertEqual(
            [(event["type"], event.get("delta")) for event in coalesced],
            [
                ("RUN_STARTED", None),
                ("TEXT_MESSAGE_START", None),
                ("TEXT_MESSAGE_CONTENT", "AB"),
                ("TEXT_MESSAGE_END", None),
                ("TOOL_CALL_START", None),
                ("TOOL_CALL_ARGS", "{}"),
                ("TOOL_CALL_END", None),
            ],
        )
