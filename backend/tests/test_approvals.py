from __future__ import annotations

import asyncio
import json
import unittest

from backend.agui import translate_agent_events
from backend.approvals import ApprovalBroker
from backend.runners.claude_approval_bridge import ClaudeApprovalBridge
from backend.runners.codex_app_server import _handle_server_request


class ApprovalBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_emits_expired_before_runner_error(self) -> None:
        broker = ApprovalBroker(timeout_seconds=0.01)

        async def runner():
            await broker.request(
                thread_id="thread-timeout",
                run_id="run-timeout",
                agent_kind="k_agent",
                category="local_tool",
                title="Allow?",
                message="timeout test",
            )
            if False:
                yield {}

        stream = broker.stream(
            runner(), thread_id="thread-timeout", run_id="run-timeout"
        )
        requested = await anext(stream)
        self.assertEqual(requested["type"], "approval_request")
        resolved = await anext(stream)
        self.assertEqual(resolved["type"], "approval_resolved")
        self.assertEqual(resolved["payload"]["action"], "expired")
        with self.assertRaisesRegex(RuntimeError, "Human approval timed out"):
            await anext(stream)

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
        self.assertTrue(await broker.is_pending(
            request_id, thread_id="thread-1", run_id="run-1"
        ))

        self.assertTrue(await broker.resolve(
            request_id,
            thread_id="thread-1",
            run_id="run-1",
            decision={"action": "approve", "remember": False},
        ))
        resolved = await anext(stream)
        resumed = await anext(stream)
        self.assertEqual(resolved["type"], "approval_resolved")
        self.assertFalse(await broker.is_pending(
            request_id, thread_id="thread-1", run_id="run-1"
        ))
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

    async def test_claude_permission_bridge_uses_the_same_broker_contract(self) -> None:
        broker = ApprovalBroker(timeout_seconds=1)
        hold_runner = asyncio.Event()

        async def runner():
            await hold_runner.wait()
            if False:
                yield {}

        stream = broker.stream(runner(), thread_id="thread-cc", run_id="run-cc")
        next_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        async with ClaudeApprovalBridge(
            broker=broker, thread_id="thread-cc", run_id="run-cc"
        ) as bridge:
            config = bridge.child_env()
            reader, writer = await asyncio.open_connection(
                config["K_AGENT_APPROVAL_HOST"],
                int(config["K_AGENT_APPROVAL_PORT"]),
            )
            writer.write((json.dumps({
                "token": config["K_AGENT_APPROVAL_TOKEN"],
                "toolName": "Bash",
                "input": {"command": "npm test"},
            }) + "\n").encode())
            await writer.drain()

            requested = await next_event
            self.assertEqual(requested["type"], "approval_request")
            self.assertEqual(requested["payload"]["agentKind"], "claude_code")
            self.assertTrue(await broker.resolve(
                requested["payload"]["id"],
                thread_id="thread-cc",
                run_id="run-cc",
                decision={"action": "approve", "remember": True},
            ))
            self.assertEqual((await anext(stream))["type"], "approval_resolved")
            decision = json.loads(await reader.readline())
            self.assertEqual(decision["behavior"], "allow")
            self.assertEqual(decision["updatedInput"], {"command": "npm test"})
            writer.close()
            await writer.wait_closed()
        await stream.aclose()

    async def test_all_builtin_agents_share_one_agui_approval_activity(self) -> None:
        for agent_kind in ("k_agent", "claude_code", "codex"):
            async def internal_events():
                yield {
                    "type": "approval_request",
                    "payload": {
                        "id": f"approval-{agent_kind}",
                        "threadId": "thread-1",
                        "runId": "run-1",
                        "agentKind": agent_kind,
                        "category": "tool",
                        "title": "Allow?",
                        "message": "Needs confirmation",
                        "detail": {},
                    },
                }
                yield {
                    "type": "approval_resolved",
                    "payload": {
                        "id": f"approval-{agent_kind}",
                        "threadId": "thread-1",
                        "runId": "run-1",
                        "action": "approve",
                    },
                }

            translated = [
                event async for event in translate_agent_events(
                    internal_events(), thread_id="thread-1", run_id="run-1"
                )
            ]
            activities = [
                event for event in translated if event.type == "ACTIVITY_SNAPSHOT"
            ]
            self.assertEqual(
                [
                    (event.activity_type, event.message_id, event.content.get("action"))
                    for event in activities
                ],
                [
                    ("approval", f"approval-{agent_kind}", None),
                    ("approval", f"approval-{agent_kind}", "approve"),
                ],
            )

    async def test_agui_activity_arrives_before_human_resolution(self) -> None:
        """The public AG-UI iterator must expose the card while the tool is blocked."""

        broker = ApprovalBroker(timeout_seconds=1)

        async def runner():
            await broker.request(
                thread_id="thread-live",
                run_id="run-live",
                agent_kind="k_agent",
                category="local_tool",
                title="Allow Bash?",
                message="live delivery",
            )
            if False:
                yield {}

        events = translate_agent_events(
            broker.stream(runner(), thread_id="thread-live", run_id="run-live"),
            thread_id="thread-live",
            run_id="run-live",
        )
        self.assertEqual((await anext(events)).type, "RUN_STARTED")
        activity = await asyncio.wait_for(anext(events), timeout=0.1)
        self.assertEqual(activity.type, "ACTIVITY_SNAPSHOT")
        self.assertEqual(activity.activity_type, "approval")
        self.assertEqual(activity.content["status"], "pending")
        self.assertTrue(await broker.is_pending(
            activity.message_id, thread_id="thread-live", run_id="run-live"
        ))
        await events.aclose()

    async def test_live_tool_output_is_forwarded_as_custom_delta(self) -> None:
        async def internal_events():
            yield {
                "type": "tool_output",
                "payload": {
                    "toolCallId": "tool-1",
                    "stream": "stdout",
                    "delta": "https://example.test/authorize\n",
                },
            }

        translated = [event async for event in translate_agent_events(
            internal_events(), thread_id="thread-1", run_id="run-1"
        )]
        custom = [event for event in translated if event.type == "CUSTOM"]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0].name, "tool_output_delta")
        self.assertEqual(custom[0].value["toolCallId"], "tool-1")


if __name__ == "__main__":
    unittest.main()
