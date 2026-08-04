from __future__ import annotations

import unittest

from backend.approvals import ApprovalBroker
from backend.runners.codex_app_server import _handle_server_request


class ApprovalBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_is_streamed_and_resolution_resumes_runner(self) -> None:
        broker = ApprovalBroker(timeout_seconds=1)

        async def runner():
            decision = await broker.request(
                thread_id="thread-1",
                run_id="run-1",
                agent_kind="k_agent",
                category="mcp_tool",
                title="Allow tool?",
                message="Needs confirmation",
                detail={"toolName": "lookup"},
            )
            yield {"type": "status", "payload": {"message": decision["action"]}}

        stream = broker.stream(runner(), thread_id="thread-1", run_id="run-1")
        requested = await anext(stream)
        request_id = requested["payload"]["id"]
        self.assertEqual(requested["type"], "approval_request")

        self.assertTrue(await broker.resolve(
            request_id,
            thread_id="thread-1",
            run_id="run-1",
            decision={"action": "approve", "remember": False},
        ))
        resolved = await anext(stream)
        resumed = await anext(stream)
        self.assertEqual(resolved["type"], "approval_resolved")
        self.assertEqual(resumed["payload"]["message"], "approve")
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    async def test_resolution_rejects_wrong_run_scope(self) -> None:
        broker = ApprovalBroker(timeout_seconds=1)

        async def runner():
            await broker.request(
                thread_id="thread-1",
                run_id="run-1",
                agent_kind="codex",
                category="command",
                title="Allow?",
                message="scope test",
            )
            if False:
                yield {}

        stream = broker.stream(runner(), thread_id="thread-1", run_id="run-1")
        requested = await anext(stream)
        self.assertFalse(await broker.resolve(
            requested["payload"]["id"],
            thread_id="thread-1",
            run_id="another-run",
            decision={"action": "approve"},
        ))
        await stream.aclose()

    async def test_codex_user_input_approval_returns_matching_option(self) -> None:
        broker = ApprovalBroker(timeout_seconds=1)

        async def runner():
            response = await _handle_server_request(
                method="item/tool/requestUserInput",
                params={
                    "questions": [
                        {
                            "id": "approval",
                            "question": "Allow this MCP tool call?",
                            "options": [
                                {"label": "Accept", "description": "Continue"},
                                {"label": "Decline", "description": "Stop"},
                            ],
                        }
                    ]
                },
                broker=broker,
                public_thread_id="thread-1",
                run_id="run-1",
            )
            yield {"type": "response", "payload": response}

        stream = broker.stream(runner(), thread_id="thread-1", run_id="run-1")
        requested = await anext(stream)
        self.assertEqual(requested["payload"]["category"], "user_input")
        self.assertTrue(await broker.resolve(
            requested["payload"]["id"],
            thread_id="thread-1",
            run_id="run-1",
            decision={"action": "approve", "remember": False},
        ))
        self.assertEqual((await anext(stream))["type"], "approval_resolved")
        response = await anext(stream)
        self.assertEqual(
            response["payload"]["answers"],
            {"approval": {"answers": ["Accept"]}},
        )

    async def test_codex_remembered_command_approval_maps_to_session(self) -> None:
        broker = ApprovalBroker(timeout_seconds=1)

        async def runner():
            response = await _handle_server_request(
                method="item/commandExecution/requestApproval",
                params={"command": "pwd", "reason": "Inspect the workspace"},
                broker=broker,
                public_thread_id="thread-1",
                run_id="run-1",
            )
            yield {"type": "response", "payload": response}

        stream = broker.stream(runner(), thread_id="thread-1", run_id="run-1")
        requested = await anext(stream)
        self.assertTrue(await broker.resolve(
            requested["payload"]["id"],
            thread_id="thread-1",
            run_id="run-1",
            decision={"action": "approve", "remember": True},
        ))
        await anext(stream)
        response = await anext(stream)
        self.assertEqual(response["payload"], {"decision": "acceptForSession"})


if __name__ == "__main__":
    unittest.main()
