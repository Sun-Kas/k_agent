from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from access_layer.schemas import ChatMessage
from access_layer.sessions.history import events_from_records, is_public_event, messages_from_records
from access_layer.sessions.migrate_history import (
    migrate_all_sessions,
    migrate_session_dir,
    migrate_session_record,
)
from access_layer.sessions.store import SessionStore
from access_layer.storage import FileStorage
from backend.agui import translate_agent_events


def message(message_id: str, role: str, content: str, run_id: str | None = None) -> dict:
    return {
        "id": message_id,
        "role": role,
        "content": content,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "meta": {"runId": run_id} if run_id else None,
        "toolCalls": [],
        "attachments": [],
    }


class HistoryMigrationTests(unittest.TestCase):
    def test_pure_migration_filters_observability_and_preserves_order(self) -> None:
        payload = {
            "id": "s1",
            "title": "history",
            "messages": [message("u1", "user", "你好"), message("a1", "assistant", "回答", "r1")],
            "trace": [],
            "tasks": [],
            "thinking": [],
            "events": [
                {"type": "RUN_STARTED", "threadId": "s1", "runId": "r1"},
                {"type": "CUSTOM", "name": "status", "value": {"message": "思考中"}},
                {"type": "CUSTOM", "name": "trace", "value": {"entry": "agent:start"}},
                {
                    "type": "CUSTOM",
                    "name": "approval_request",
                    "value": {"id": "approval-1", "runId": "r1", "title": "Allow?"},
                },
                {"type": "TEXT_MESSAGE_START", "messageId": "a1"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "回"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "答"},
                {"type": "TEXT_MESSAGE_END", "messageId": "a1"},
                {"type": "STATE_SNAPSHOT", "snapshot": {"secret": True}},
                {"type": "RUN_FINISHED", "threadId": "s1", "runId": "r1"},
            ],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        migrated = migrate_session_record(payload)
        self.assertFalse({"messages", "events", "trace", "tasks", "thinking"} & migrated.metadata.keys())
        events = events_from_records(migrated.history_records)
        self.assertEqual(events[0]["type"], "input_message")
        self.assertEqual(
            [event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"],
            ["回答"],
        )
        self.assertFalse(any(event.get("type") in {"STATE_SNAPSHOT", "MESSAGES_SNAPSHOT"} for event in events))
        self.assertFalse(any(event.get("name") in {"status", "trace", "tool_output_delta"} for event in events))
        approval = next(event for event in events if event["type"] == "ACTIVITY_SNAPSHOT")
        self.assertEqual(approval["content"]["status"], "pending")
        self.assertEqual([(item.role, item.content) for item in messages_from_records(migrated.history_records)], [("user", "你好"), ("assistant", "回答")])

    def test_directory_migration_is_idempotent_and_keeps_backup(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            session_dir = root / "s1"
            session_dir.mkdir(parents=True)
            (session_dir / "s1.json").write_text(json.dumps({
                "id": "s1", "title": "x", "messages": [message("u1", "user", "hi")],
                "events": [], "updatedAt": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
            self.assertTrue(migrate_session_dir(session_dir))
            self.assertTrue((session_dir / "session.json").is_file())
            self.assertTrue((session_dir / "history.jsonl").is_file())
            self.assertTrue((session_dir / "s1.json.bak").is_file())
            self.assertFalse(migrate_session_dir(session_dir))
            report = migrate_all_sessions(root)
            self.assertEqual((report.migrated, report.failed), (0, 0))

    def test_message_only_legacy_tool_pair_becomes_standard_tool_events(self) -> None:
        assistant = message("legacy-assistant", "assistant", "", "r1")
        assistant["toolCalls"] = [{"id": "call-1", "name": "Read", "arguments": '{"path":"a"}'}]
        result = message("legacy-result", "tool", "ok", "r1")
        result["meta"] = {"runId": "r1", "toolCallId": "call-1", "toolName": "Read"}
        migrated = migrate_session_record({
            "id": "s1",
            "messages": [message("u1", "user", "read", "r1"), assistant, result],
            "events": [],
        })
        projected = messages_from_records(migrated.history_records)
        self.assertEqual([item.role for item in projected], ["user", "assistant", "tool"])
        self.assertEqual(projected[1].tool_calls[0].arguments, '{"path":"a"}')
        self.assertEqual(projected[2].content, "ok")
        event_types = [event["type"] for event in events_from_records(migrated.history_records)]
        self.assertIn("TOOL_CALL_START", event_types)
        self.assertIn("TOOL_CALL_RESULT", event_types)

    def test_messages_before_retained_event_window_stay_before_that_window(self) -> None:
        old_call = message("toolcall-old", "assistant", "", "r1")
        old_call["toolCalls"] = [{"id": "old-call", "name": "Read", "arguments": "{}"}]
        old_result = message("old-result", "tool", "old output", "r1")
        old_result["meta"] = {"runId": "r1", "toolCallId": "old-call", "toolName": "Read"}
        payload = {
            "id": "s1",
            "messages": [
                old_call,
                old_result,
                message("u2", "user", "next"),
                message("a2", "assistant", "answer", "r2"),
            ],
            "events": [
                {"type": "RUN_STARTED", "threadId": "s1", "runId": "r2"},
                {"type": "TEXT_MESSAGE_START", "messageId": "a2"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a2", "delta": "answer"},
                {"type": "TEXT_MESSAGE_END", "messageId": "a2"},
                {"type": "RUN_FINISHED", "threadId": "s1", "runId": "r2"},
            ],
        }
        migrated = migrate_session_record(payload)
        events = events_from_records(migrated.history_records)
        self.assertLess(
            next(i for i, event in enumerate(events) if event["type"] == "TOOL_CALL_RESULT"),
            next(i for i, event in enumerate(events) if event["type"] == "RUN_STARTED"),
        )
        self.assertLess(
            next(i for i, event in enumerate(events) if event["type"] == "input_message"),
            next(i for i, event in enumerate(events) if event["type"] == "RUN_STARTED"),
        )

    def test_multiple_legacy_thinking_blocks_get_distinct_reasoning_ids(self) -> None:
        migrated = migrate_session_record({
            "id": "s1",
            "messages": [],
            "events": [
                {"type": "THINKING_START", "title": "one"},
                {"type": "THINKING_TEXT_MESSAGE_START"},
                {"type": "THINKING_TEXT_MESSAGE_CONTENT", "delta": "a"},
                {"type": "THINKING_TEXT_MESSAGE_END"},
                {"type": "THINKING_END"},
                {"type": "THINKING_START", "title": "two"},
                {"type": "THINKING_TEXT_MESSAGE_START"},
                {"type": "THINKING_TEXT_MESSAGE_CONTENT", "delta": "b"},
                {"type": "THINKING_TEXT_MESSAGE_END"},
                {"type": "THINKING_END"},
            ],
        })
        starts = [
            event for event in events_from_records(migrated.history_records)
            if event["type"] == "REASONING_START"
        ]
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[0]["messageId"], starts[1]["messageId"])


class ProtocolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_trace_are_dropped_and_private_control_never_becomes_public(self) -> None:
        async def internal_events():
            yield {"type": "status", "payload": {"message": "working"}}
            yield {"type": "trace", "payload": {"entry": "internal"}}
            yield {"type": "cli_session", "payload": {"kind": "codex", "sessionId": "native"}}
            yield {"type": "context_state", "payload": {"autoCompacted": False}}
            yield {"type": "final", "payload": {}}

        translated = [
            event.model_dump(by_alias=True, mode="json", exclude_none=True)
            async for event in translate_agent_events(internal_events(), "s1", "r1")
        ]
        self.assertEqual(
            [event["type"] for event in translated],
            ["RUN_STARTED", "CUSTOM", "CUSTOM", "RUN_FINISHED"],
        )
        private = [event for event in translated if event["type"] == "CUSTOM"]
        self.assertEqual(
            [event["name"] for event in private],
            ["__private_cli_session", "__private_context_state"],
        )
        self.assertTrue(is_public_event(translated[0]))
        self.assertFalse(any(is_public_event(event) for event in private))


class HistoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_persists_metadata_and_history_separately(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = FileStorage(tmp)
            store = SessionStore(storage)
            await store.create_session(session_id="s1")
            user = ChatMessage(
                id="u1", role="user", content="hi", createdAt=datetime.now(timezone.utc)
            )
            await store.save_run_start(
                "s1", [user], run_id="r1", mcp_server_ids=[], skill_ids=[]
            )
            for event in [
                {"type": "RUN_STARTED", "threadId": "s1", "runId": "r1"},
                {"type": "TEXT_MESSAGE_START", "messageId": "a1", "role": "assistant"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "hel"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "lo"},
                {"type": "TEXT_MESSAGE_END", "messageId": "a1"},
                {"type": "RUN_FINISHED", "threadId": "s1", "runId": "r1"},
            ]:
                await store.append_event("s1", event)

            metadata = await storage.read_json("sessions/s1/session.json")
            assert isinstance(metadata, dict)
            self.assertFalse({"messages", "events", "trace", "tasks", "thinking"} & metadata.keys())
            history = await storage.read_text_range("sessions/s1/history.jsonl")
            self.assertGreaterEqual(len(history), 3)
            reloaded = await SessionStore(storage).get("s1")
            assert reloaded is not None
            self.assertEqual([(item.role, item.content) for item in reloaded.messages], [("user", "hi"), ("assistant", "hello")])
            self.assertEqual(reloaded.events[0]["type"], "input_message")

    async def test_compact_state_anchors_to_history_seq(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = FileStorage(tmp)
            store = SessionStore(storage)
            await store.create_session(session_id="s1")
            for run_id, user_id, assistant_id, prompt, answer in [
                ("r1", "u1", "a1", "one", "first"),
                ("r2", "u2", "a2", "two", "second"),
            ]:
                await store.save_run_start(
                    "s1",
                    [ChatMessage(id=user_id, role="user", content=prompt, createdAt=datetime.now(timezone.utc))],
                    run_id=run_id,
                    mcp_server_ids=[],
                    skill_ids=[],
                )
                for event in [
                    {"type": "RUN_STARTED", "threadId": "s1", "runId": run_id},
                    {"type": "TEXT_MESSAGE_START", "messageId": assistant_id, "role": "assistant"},
                    {"type": "TEXT_MESSAGE_CONTENT", "messageId": assistant_id, "delta": answer},
                    {"type": "TEXT_MESSAGE_END", "messageId": assistant_id},
                    {"type": "RUN_FINISHED", "threadId": "s1", "runId": run_id},
                ]:
                    await store.append_event("s1", event)
            await store.commit_context_compaction(
                "s1",
                {
                    "proposalId": "proposal-1",
                    "expectedGeneration": 0,
                    "expectedRevision": 0,
                    "boundary": {
                        "id": "compact-1",
                        "coveredThroughMessageId": "a1",
                        "trigger": "auto",
                        "sourceRunId": "r2",
                    },
                    "summary": {
                        "formatVersion": 1,
                        "text": "first turn summary",
                        "modelId": "test-model",
                        "inputTokens": 10,
                        "outputTokens": 3,
                    },
                    "toolReplacements": [],
                    "workingSet": {},
                },
                None,
            )
            active, summary = await store.provider_context("s1")
            self.assertEqual(summary, "first turn summary")
            self.assertEqual([(item.id, item.content) for item in active], [("u2", "two"), ("a2", "second")])
            state = await storage.read_json("sessions/s1/context/k_agent.json")
            assert isinstance(state, dict)
            self.assertEqual(state["generation"], 1)
            self.assertEqual(state["revision"], 1)
            self.assertIsInstance(state["boundary"]["coveredThroughSeq"], int)
            self.assertEqual(state["boundary"]["coveredThroughMessageId"], "a1")
            self.assertTrue(str(state["boundary"]["coveredPrefixDigest"]).startswith("sha256:"))
            branch = await store.fork_session("s1")
            assert branch is not None
            branch_active, branch_summary = await store.provider_context(branch.id)
            self.assertEqual(branch_summary, "first turn summary")
            self.assertEqual(
                [(item.id, item.content) for item in branch_active],
                [("u2", "two"), ("a2", "second")],
            )


if __name__ == "__main__":
    unittest.main()
