from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from access_layer.schemas import ChatMessage
from access_layer.concurrency import RequestConcurrencyLimiter
from access_layer.gateway import AgentAccessLayer
from access_layer.sessions.context_store import (
    ContextStateConflict,
    ContextStateStore,
)
from access_layer.sessions.history import make_record
from access_layer.sessions.store import SessionStore
from access_layer.sessions.store import ResumeConflictError
from access_layer.storage.file import FileStorage
from backend.context import generate_compaction, sanitize_provider_messages
from backend.context.compact import SUMMARY_HEADINGS


def summary_text() -> str:
    return "# Conversation State\n\n" + "\n\n".join(
        f"## {heading}\nkept" for heading in SUMMARY_HEADINGS
    )


def proposal(message_id: str = "u1") -> dict:
    return {
        "proposalId": "p1",
        "expectedGeneration": 0,
        "expectedRevision": 0,
        "boundary": {"id": "b1", "coveredThroughMessageId": message_id, "trigger": "manual"},
        "summary": {"formatVersion": 1, "text": summary_text(), "modelId": "m"},
        "toolReplacements": [],
        "workingSet": {},
    }


class _Completions:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("context length exceeded")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=summary_text()))],
            usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45),
        )


class _Client:
    def __init__(self, failures: int = 0) -> None:
        self.chat = SimpleNamespace(completions=_Completions(failures))


class ContextStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_cas_idempotency_invalidation_and_restart_projection(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = FileStorage(tmp)
            store = ContextStateStore(storage)
            user = ChatMessage(
                id="u1", role="user", content="original",
                createdAt=datetime.now(timezone.utc),
            )
            records = [make_record(
                seq=1, session_id="s1", run_id="r1", kind="input_message",
                message=user.model_dump(by_alias=True, mode="json"),
            )]
            first = await store.commit_compaction("s1", records, proposal(), None)
            duplicate = await store.commit_compaction("s1", records, proposal(), None)
            self.assertEqual(first["generation"], 1)
            self.assertEqual(duplicate["generation"], 1)

            appended = list(records)
            for index in range(2, 22):
                appended.append(make_record(
                    seq=index, session_id="s1", run_id=f"r{index}", kind="input_message",
                    message=ChatMessage(
                        id=f"u{index}", role="user", content=f"later {index}",
                        createdAt=datetime.now(timezone.utc),
                    ).model_dump(by_alias=True, mode="json"),
                ))
            active, restarted_state = await ContextStateStore(storage).active_view("s1", appended)
            self.assertEqual(restarted_state["generation"], 1)
            self.assertEqual([item.id for item in active], [f"u{index}" for index in range(2, 22)])

            edited = [*appended, make_record(
                seq=22, session_id="s1", run_id="edit", kind="history_mutation",
                mutation={
                    "type": "replace_message",
                    "message": user.model_copy(update={"content": "changed"}).model_dump(
                        by_alias=True, mode="json"
                    ),
                },
            )]
            full, invalidated = await ContextStateStore(storage).active_view("s1", edited)
            self.assertEqual(invalidated["generation"], 0)
            self.assertEqual(full[0].content, "changed")
            self.assertEqual(len(full), 21)

    async def test_stale_revision_and_failure_breaker(self) -> None:
        store = ContextStateStore(None)
        for _ in range(3):
            state = await store.record_failure("s1", code="timeout", automatic=True)
        self.assertTrue(state["failureState"]["autoDisabled"])
        self.assertEqual(state["generation"], 0)
        with self.assertRaises(ContextStateConflict):
            await store.commit_patch("s1", {
                "proposalId": "old", "expectedRevision": 0, "toolReplacements": [],
            })
        public = store.public_status(state)
        self.assertNotIn("summary", public)
        self.assertNotIn("toolReplacements", public)
        self.assertIsInstance(public["pendingContinuation"], bool)

    async def test_restart_recovery_continues_same_run_before_new_input(self) -> None:
        store = SessionStore(storage=None)
        await store.create_session(session_id="s-recover")
        await store.save_run_start(
            "s-recover",
            [ChatMessage(
                id="u1", role="user", content="continue",
                createdAt=datetime.now(timezone.utc),
            )],
            run_id="r1", mcp_server_ids=[], skill_ids=[], model_id="m1",
        )
        await store.append_event(
            "s-recover", {"type": "RUN_STARTED", "threadId": "s-recover", "runId": "r1"}
        )
        checkpoint = {
            "version": 1, "kind": "context_continuation", "contextGeneration": 1,
            "iteration": 0, "modelMessages": [],
            "resumeContext": {
                "publicRunId": "r1", "modelId": "m1", "mcpServerIds": [],
                "skillIds": [], "agentOptions": {},
            },
        }
        await store.commit_context_compaction("s-recover", proposal(), checkpoint)

        class Backend:
            async def stream(self, _payload, _request_id):
                yield {"type": "RUN_STARTED", "threadId": "s-recover", "runId": "r1"}
                yield {"type": "TEXT_MESSAGE_START", "messageId": "a1", "role": "assistant"}
                yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "done"}
                yield {"type": "TEXT_MESSAGE_END", "messageId": "a1"}
                yield {"type": "RUN_FINISHED", "threadId": "s-recover", "runId": "r1"}

        class Catalog:
            def selected_runtime(self, _mcp_ids, _skill_ids):
                return [], []

        layer = AgentAccessLayer(
            session_store=store,
            request_limiter=RequestConcurrencyLimiter(1, 1),
            agent_backend_client=Backend(),
            runtime_catalog=Catalog(),
        )
        await layer.recover_pending_context_continuations()
        status = await store.context_status("s-recover")
        self.assertFalse(status["pendingContinuation"])
        session = await store.get("s-recover")
        self.assertEqual(session.messages[-1].content, "done")
        self.assertEqual(
            sum(1 for event in session.events if event.get("type") == "RUN_STARTED"), 1
        )

    async def test_hitl_resume_rejects_changed_context_generation(self) -> None:
        store = SessionStore(storage=None)
        await store.create_session(session_id="s-hitl")
        await store.save_run_start(
            "s-hitl",
            [ChatMessage(
                id="u1", role="user", content="edit",
                createdAt=datetime.now(timezone.utc),
            )],
            run_id="r1", mcp_server_ids=[], skill_ids=[],
        )
        await store.persist_interrupt("s-hitl", {
            "type": "ACTIVITY_SNAPSHOT",
            "content": {
                "id": "i1", "runId": "r1", "requestHash": "sha256:test",
                "_checkpoint": {"kind": "react_tool_boundary", "resumeContext": {}},
            },
        })
        await store.commit_context_compaction("s-hitl", proposal(), None)
        with self.assertRaisesRegex(ResumeConflictError, "context changed"):
            await store.prepare_resume(
                "s-hitl",
                [{"interruptId": "i1", "status": "resolved", "payload": {"approved": True}}],
                resume_run_id="r2", resume_context={},
            )


class CompactGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_has_no_tools_and_records_usage(self) -> None:
        client = _Client()
        messages = [
            {"role": "user", "content": f"turn {index}", "_message_id": f"u{index}"}
            for index in range(8)
        ]
        result = await generate_compaction(
            client=client, model_config={
                "id": "compact", "model": "provider-model", "contextWindow": 128_000,
                "maxOutputTokens": 8_000, "contextSafetyTokens": 4_096,
            },
            messages=messages, context_state={"generation": 0, "revision": 0},
            source_run_id="r1", trigger="manual", continuation=True,
        )
        call = client.chat.completions.calls[0]
        self.assertNotIn("tools", call)
        self.assertFalse(call["stream"])
        self.assertEqual(result.proposal["summary"]["inputTokens"], 123)
        self.assertIsNotNone(result.continuation_checkpoint)
        self.assertEqual(result.remaining_messages[-1]["_message_id"], "u7")

    async def test_prompt_too_long_retry_moves_boundary_back_by_whole_round(self) -> None:
        client = _Client(failures=1)
        messages = [
            {"role": "user", "content": "x" * 100, "_message_id": f"u{index}"}
            for index in range(12)
        ]
        result = await generate_compaction(
            client=client, model_config={
                "id": "compact", "model": "provider-model", "contextWindow": 128_000,
                "maxOutputTokens": 8_000, "contextSafetyTokens": 4_096,
            },
            messages=messages, context_state={"generation": 0, "revision": 0},
            source_run_id="r1", trigger="manual", continuation=False,
        )
        self.assertEqual(len(client.chat.completions.calls), 2)
        self.assertNotEqual(
            result.proposal["boundary"]["coveredThroughMessageId"], "u10"
        )
        self.assertGreater(len(result.remaining_messages), 1)

    def test_private_projection_keys_never_reach_provider(self) -> None:
        cleaned = sanitize_provider_messages([{
            "role": "user", "content": "hello", "_message_id": "u1",
            "_request_context": True,
        }])
        self.assertEqual(cleaned, [{"role": "user", "content": "hello"}])


if __name__ == "__main__":
    unittest.main()
