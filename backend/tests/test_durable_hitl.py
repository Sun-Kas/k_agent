from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ag_ui.core import RunAgentInput

from access_layer.gateway import AgentAccessLayer
from access_layer.sessions.store import (
    OpenInterruptError,
    ResumeConflictError,
    SessionStore,
)
from access_layer.teams.runtime import TeamApprovalInterrupted, TeamRuntime
from backend.storage import FileStorage
from backend.api.schemas import ChatMessage
from datetime import datetime, timezone


class DurableHitlStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_replays_tool_boundary_to_persist_complete_pair(self) -> None:
        store = SessionStore()
        session = await store.create_session(session_id="thread-tool-pair")
        await store.save_run_start(
            session.id,
            [ChatMessage(
                id="user", role="user", content="run tool",
                createdAt=datetime.now(timezone.utc),
            )],
            run_id="run-1", mcp_server_ids=[], skill_ids=[],
        )
        for event in (
            {"type": "RUN_STARTED", "threadId": session.id, "runId": "run-1"},
            {"type": "TOOL_CALL_START", "toolCallId": "call-1", "toolCallName": "Write"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": "{\"path\":\"a.txt\"}"},
            {"type": "RUN_FINISHED", "threadId": session.id, "runId": "run-1", "outcome": {"type": "interrupt"}},
            {"type": "RUN_STARTED", "threadId": session.id, "runId": "run-2"},
            {"type": "TOOL_CALL_START", "toolCallId": "call-1", "toolCallName": "Write"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": "{\"path\":\"a.txt\"}"},
            {"type": "TOOL_CALL_RESULT", "toolCallId": "call-1", "messageId": "result-1", "content": "ok"},
        ):
            await store.append_event(session.id, event)
        loaded = await store.get(session.id)
        self.assertEqual(loaded.messages[-2].tool_calls[0].id, "call-1")
        self.assertEqual(loaded.messages[-1].meta.tool_call_id, "call-1")

    async def test_interrupt_is_durable_before_public_projection_and_resumes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = FileStorage(Path(temp_dir))
            store = SessionStore(storage)
            session = await store.create_session(session_id="thread-1")
            event = {
                "type": "ACTIVITY_SNAPSHOT",
                "messageId": "interrupt-1",
                "activityType": "approval",
                "replace": True,
                "content": {
                    "id": "interrupt-1",
                    "threadId": session.id,
                    "runId": "run-1",
                    "agentKind": "k_agent",
                    "category": "local_tool",
                    "title": "允许调用 Bash？",
                    "message": "需要确认",
                    "detail": {
                        "toolName": "Bash",
                        "callId": "call-1",
                        "arguments": {"command": "pwd"},
                        "source": "local",
                    },
                    "toolCallId": "call-1",
                    "requestHash": "sha256:test",
                    "status": "pending",
                    "_checkpoint": {
                        "version": 1,
                        "kind": "react_tool_boundary",
                        "requestHash": "sha256:test",
                        "messages": [],
                        "iteration": 0,
                        "pendingIndex": 0,
                        "pendingCalls": [{
                            "id": "call-1",
                            "name": "Bash",
                            "arguments": "{\"command\":\"pwd\"}",
                        }],
                        "modelMessages": [],
                    },
                },
            }

            public_event = await store.persist_interrupt(session.id, event)
            self.assertNotIn("_checkpoint", public_event["content"])
            self.assertEqual(session.open_interrupt_ids, ["interrupt-1"])
            with self.assertRaises(OpenInterruptError):
                await store.ensure_accepts_new_input(session.id)

            # A fresh store proves that the approval/checkpoint is not only held
            # by the process that produced the original run.
            reloaded = SessionStore(storage)
            loaded = await reloaded.get(session.id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.open_interrupt_ids, ["interrupt-1"])
            open_interrupts = await reloaded.list_open_interrupts(session.id)
            self.assertEqual(open_interrupts[0]["status"], "pending")
            self.assertNotIn("checkpoint", open_interrupts[0])

            records = await reloaded.prepare_resume(
                session.id,
                [{
                    "interruptId": "interrupt-1",
                    "status": "resolved",
                    "payload": {"approved": True, "scope": "once"},
                }],
                resume_run_id="run-2",
            )
            self.assertEqual(records[0]["checkpoint"]["kind"], "react_tool_boundary")
            with self.assertRaises(ResumeConflictError):
                await reloaded.prepare_resume(
                    session.id,
                    [{
                        "interruptId": "interrupt-1",
                        "status": "resolved",
                        "payload": {"approved": False},
                    }],
                    resume_run_id="run-3",
                )

            await reloaded.finish_resume(
                session.id, ["interrupt-1"], succeeded=True
            )
            self.assertEqual((await reloaded.get(session.id)).open_interrupt_ids, [])
            self.assertEqual(await reloaded.list_open_interrupts(session.id), [])

    async def test_cli_provider_interrupts_survive_access_layer_restart(self) -> None:
        for agent_kind in ("claude_code", "codex"):
            with self.subTest(agent_kind=agent_kind), tempfile.TemporaryDirectory() as temp_dir:
                storage = FileStorage(Path(temp_dir))
                store = SessionStore(storage)
                session = await store.create_session(session_id=f"thread-{agent_kind}")
                interrupt_id = f"interrupt-{agent_kind}"
                request_hash = f"sha256:{agent_kind}"
                public_event = await store.persist_interrupt(session.id, {
                    "type": "ACTIVITY_SNAPSHOT",
                    "messageId": interrupt_id,
                    "activityType": "approval",
                    "replace": True,
                    "content": {
                        "id": interrupt_id,
                        "threadId": session.id,
                        "runId": "run-1",
                        "agentKind": agent_kind,
                        "category": "tool",
                        "title": "Provider approval",
                        "message": "Confirm",
                        "detail": {
                            "source": f"{agent_kind}_provider",
                            "toolName": "Bash",
                            "input": {"command": "pwd"},
                        },
                        "requestHash": request_hash,
                        "status": "pending",
                        "_checkpoint": {
                            "version": 1,
                            "kind": "restart_from_context",
                            "requestHash": request_hash,
                            "resumeContext": {
                                "agentKind": agent_kind,
                                "agentOptions": {"permissionMode": "default"},
                            },
                        },
                    },
                })
                self.assertNotIn("_checkpoint", public_event["content"])

                # A new Access Layer process can still list and atomically
                # claim either provider's server-owned checkpoint.
                restarted = SessionStore(storage)
                open_interrupts = await restarted.list_open_interrupts(session.id)
                self.assertEqual(open_interrupts[0]["agentKind"], agent_kind)
                self.assertNotIn("checkpoint", open_interrupts[0])
                records = await restarted.prepare_resume(
                    session.id,
                    [{
                        "interruptId": interrupt_id,
                        "status": "resolved",
                        "payload": {"approved": True, "scope": "once"},
                    }],
                    resume_run_id="run-2",
                    resume_context={
                        "agentKind": agent_kind,
                        "agentOptions": {"permissionMode": "default"},
                    },
                )
                self.assertEqual(records[0]["agentKind"], agent_kind)
                self.assertEqual(
                    records[0]["checkpoint"]["kind"], "restart_from_context"
                )
                self.assertEqual(records[0]["requestHash"], request_hash)

    async def test_resume_must_cover_every_open_interrupt(self) -> None:
        store = SessionStore()
        session = await store.create_session(session_id="thread-many")
        for index in (1, 2):
            await store.persist_interrupt(session.id, {
                "type": "ACTIVITY_SNAPSHOT",
                "content": {
                    "id": f"interrupt-{index}",
                    "threadId": session.id,
                    "runId": "run-1",
                    "agentKind": "k_agent",
                    "requestHash": f"sha256:{index}",
                    "_checkpoint": {"version": 1, "kind": "react_tool_boundary"},
                },
            })
        with self.assertRaises(ResumeConflictError):
            await store.prepare_resume(
                session.id,
                [{
                    "interruptId": "interrupt-1",
                    "status": "cancelled",
                }],
                resume_run_id="run-2",
            )

    async def test_restart_turns_inflight_resume_into_explicit_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = FileStorage(Path(temp_dir))
            store = SessionStore(storage)
            session = await store.create_session(session_id="thread-crash")
            await store.persist_interrupt(session.id, {
                "type": "ACTIVITY_SNAPSHOT",
                "content": {
                    "id": "interrupt-crash", "threadId": session.id,
                    "runId": "run-1", "agentKind": "k_agent",
                    "requestHash": "sha256:crash",
                    "_checkpoint": {"version": 1, "kind": "react_tool_boundary"},
                },
            })
            await store.prepare_resume(
                session.id,
                [{
                    "interruptId": "interrupt-crash", "status": "resolved",
                    "payload": {"approved": True},
                }],
                resume_run_id="run-2",
            )

            restarted = SessionStore(storage)
            await restarted.get(session.id)
            self.assertEqual(
                (await restarted.list_open_interrupts(session.id))[0]["status"],
                "unknown_outcome",
            )
            with self.assertRaises(ResumeConflictError):
                await restarted.prepare_resume(
                    session.id,
                    [{
                        "interruptId": "interrupt-crash", "status": "resolved",
                        "payload": {"approved": True},
                    }],
                    resume_run_id="run-3",
                )
            records = await restarted.prepare_resume(
                session.id,
                [{
                    "interruptId": "interrupt-crash", "status": "resolved",
                    "payload": {"approved": True, "reconfirm": True},
                }],
                resume_run_id="run-3",
            )
            self.assertEqual(records[0]["status"], "resuming")

    async def test_gateway_persists_before_streaming_and_forwards_trusted_resume(self) -> None:
        store = SessionStore()

        class Guard:
            async def __aenter__(self): return None
            async def __aexit__(self, *_args): return None

        class Limiter:
            def protect(self, _session_id): return Guard()

        class Catalog:
            def selected_runtime(self, _mcp_ids, _skill_ids): return [], []

        class Backend:
            def __init__(self): self.payloads: list[dict] = []

            async def stream(self, payload, _request_id):
                self.payloads.append(payload)
                if not payload.get("resume"):
                    yield {
                        "type": "ACTIVITY_SNAPSHOT",
                        "messageId": "interrupt-gateway",
                        "activityType": "approval",
                        "replace": True,
                        "content": {
                            "id": "interrupt-gateway",
                            "threadId": payload["threadId"],
                            "runId": payload["runId"],
                            "agentKind": "k_agent",
                            "requestHash": "sha256:gateway",
                            "status": "pending",
                            "_checkpoint": {
                                "version": 1, "kind": "react_tool_boundary",
                                "messages": [], "iteration": 0, "pendingIndex": 0,
                                "pendingCalls": [{
                                    "id": "call-gateway", "name": "Write",
                                    "arguments": "{\"path\":\"note.txt\"}",
                                }],
                                "modelMessages": [],
                            },
                        },
                    }
                    yield {
                        "type": "RUN_FINISHED", "threadId": payload["threadId"],
                        "runId": payload["runId"],
                        "outcome": {"type": "interrupt", "interrupts": []},
                    }
                else:
                    yield {"type": "RUN_STARTED", "threadId": payload["threadId"], "runId": payload["runId"]}
                    yield {"type": "RUN_FINISHED", "threadId": payload["threadId"], "runId": payload["runId"]}

        backend = Backend()
        layer = AgentAccessLayer(
            session_store=store, request_limiter=Limiter(),
            agent_backend_client=backend, runtime_catalog=Catalog(),
        )
        first = RunAgentInput.model_validate({
            "threadId": "thread-gateway", "runId": "run-1", "state": {},
            "messages": [{"id": "user-1", "role": "user", "content": "write"}],
            "tools": [], "context": [],
            "forwardedProps": {"agentKind": "k_agent", "agentOptions": {"permissionMode": "default"}},
        })
        with patch("access_layer.gateway.session_workspace_dir", return_value=Path("/tmp/thread-gateway/workspace")), patch(
            "access_layer.gateway.to_managed_path", return_value="sessions/thread-gateway/workspace"
        ):
            response = await layer.run(first)
            chunks = [str(chunk) async for chunk in response.body_iterator]
        streamed = "".join(chunks)
        self.assertNotIn("_checkpoint", streamed)
        self.assertIn("interrupt-gateway", streamed)
        self.assertEqual((await store.get("thread-gateway")).open_interrupt_ids, ["interrupt-gateway"])

        second = RunAgentInput.model_validate({
            "threadId": "thread-gateway", "runId": "run-2", "state": {},
            "messages": [], "tools": [], "context": [],
            "resume": [{
                "interruptId": "interrupt-gateway", "status": "resolved",
                "payload": {"approved": True, "scope": "once"},
            }],
            "forwardedProps": {"agentKind": "k_agent", "agentOptions": {"permissionMode": "default"}},
        })
        with patch("access_layer.gateway.session_workspace_dir", return_value=Path("/tmp/thread-gateway/workspace")), patch(
            "access_layer.gateway.to_managed_path", return_value="sessions/thread-gateway/workspace"
        ):
            response = await layer.run(second)
            _ = [chunk async for chunk in response.body_iterator]
        self.assertEqual(backend.payloads[-1]["resume"][0]["interruptId"], "interrupt-gateway")
        self.assertEqual(
            backend.payloads[-1]["resumeCheckpoints"][0]["checkpoint"]["kind"],
            "react_tool_boundary",
        )
        self.assertEqual((await store.get("thread-gateway")).open_interrupt_ids, [])

    async def test_team_terminal_interrupt_is_not_submitted_as_empty_artifact(self) -> None:
        class Store:
            database_path = Path("/tmp/team-test.sqlite")

            def __init__(self): self.approvals: list[tuple] = []

            async def record_approval(self, *args): self.approvals.append(args)

            async def append_event(self, *_args): return None

        class Backend:
            async def stream(self, payload, _request_id):
                yield {
                    "type": "ACTIVITY_SNAPSHOT", "activityType": "approval",
                    "content": {
                        "id": "team-interrupt", "threadId": payload["threadId"],
                        "runId": payload["runId"], "status": "pending",
                        "_checkpoint": {"kind": "react_tool_boundary"},
                    },
                }
                yield {
                    "type": "RUN_FINISHED", "threadId": payload["threadId"],
                    "runId": payload["runId"],
                    "outcome": {"type": "interrupt", "interrupts": []},
                }

        store = Store()
        runtime = TeamRuntime(
            store=store, backend_client=Backend(), runtime_catalog=object()
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "access_layer.teams.runtime.to_managed_path", return_value="teams/t/task"
        ):
            with self.assertRaises(TeamApprovalInterrupted):
                await runtime._stream_agent(
                    team_id="team-1", task_id="task-1", agent_id="agent-1",
                    run_id="run-1", request_id="request-1", prompt="work",
                    agent={"agentKind": "k_agent", "modelId": "model", "capabilities": {}},
                    mcp_servers=[], skills=[], workspace=Path(temp_dir),
                )
        self.assertEqual(store.approvals[0][3], "approval.requested")


if __name__ == "__main__":
    unittest.main()
