from __future__ import annotations

import asyncio
import json
import unittest

from backend.agui import translate_agent_events
from backend.approvals import (
    ApprovalBroker,
    canonical_json_sha256,
    consume_resume_authorization,
)
from backend.runners.claude_approval_bridge import ClaudeApprovalBridge
from backend.runners.codex_app_server import (
    _codex_request_detail,
    _handle_server_request,
)


class ApprovalBrokerTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_resume_authorization_is_hash_bound_and_one_shot(self) -> None:
        detail = {
            "source": "claude_permission_prompt",
            "toolName": "Bash",
            "input": {"command": "pwd"},
        }
        authorization = {
            "requestHash": canonical_json_sha256({
                "target": "Bash", "source": "claude_permission_prompt",
                "serverId": None, "arguments": {"command": "pwd"},
            }),
            "decision": {
                "status": "resolved",
                "payload": {"approved": True, "scope": "once"},
            },
        }
        self.assertEqual(
            consume_resume_authorization(
                authorization, title="Claude Code 请求调用 Bash", detail=detail
            ),
            {"action": "approve", "scope": "once"},
        )
        self.assertIsNone(consume_resume_authorization(
            authorization, title="Claude Code 请求调用 Bash", detail=detail
        ))
        changed = {**detail, "input": {"command": "rm note.txt"}}
        self.assertIsNone(consume_resume_authorization(
            {**authorization, "consumed": False},
            title="Claude Code 请求调用 Bash", detail=changed,
        ))

    async def test_permission_terminates_run_without_backend_pending_state(self) -> None:
        broker = ApprovalBroker()

        async def runner():
            await broker.request(
                thread_id="thread-pending",
                run_id="run-pending",
                agent_kind="k_agent",
                category="local_tool",
                title="Allow?",
                message="pending test",
            )
            if False:
                yield {}

        stream = broker.stream(
            runner(), thread_id="thread-pending", run_id="run-pending"
        )
        requested = await anext(stream)
        self.assertEqual(requested["type"], "approval_request")
        self.assertEqual((await anext(stream))["type"], "interrupt")
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    async def test_request_is_streamed_then_original_runner_stops(self) -> None:
        broker = ApprovalBroker()

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
        self.assertEqual(requested["type"], "approval_request")
        self.assertEqual((await anext(stream))["type"], "interrupt")
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    async def test_codex_user_input_becomes_terminal_interrupt(self) -> None:
        broker = ApprovalBroker()

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
        self.assertEqual((await anext(stream))["type"], "interrupt")

    async def test_codex_command_becomes_terminal_interrupt(self) -> None:
        broker = ApprovalBroker()

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
        self.assertEqual(requested["type"], "approval_request")
        self.assertEqual((await anext(stream))["type"], "interrupt")

    async def test_codex_resume_hash_binds_semantics_but_not_replay_ids(self) -> None:
        original = {
            "threadId": "provider-thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "command": "pwd",
            "reason": "Inspect the workspace",
        }
        detail = _codex_request_detail(
            "item/commandExecution/requestApproval", original
        )
        request_hash = canonical_json_sha256({
            "target": detail["toolName"],
            "source": detail["source"],
            "serverId": None,
            "arguments": detail["arguments"],
        })
        authorization = {
            "requestHash": request_hash,
            "decision": {
                "status": "resolved",
                "payload": {"approved": True, "scope": "once"},
            },
        }
        replayed = {**original, "threadId": "provider-thread-2", "turnId": "turn-2"}
        result = await _handle_server_request(
            method="item/commandExecution/requestApproval",
            params=replayed,
            broker=ApprovalBroker(),
            public_thread_id="thread-1",
            run_id="run-2",
            resume_authorization=authorization,
        )
        self.assertEqual(result, {"decision": "accept"})

        changed_authorization = {**authorization, "consumed": False}
        with self.assertRaisesRegex(RuntimeError, "outside an active run"):
            await _handle_server_request(
                method="item/commandExecution/requestApproval",
                params={**replayed, "command": "rm note.txt"},
                broker=ApprovalBroker(),
                public_thread_id="thread-1",
                run_id="run-3",
                resume_authorization=changed_authorization,
            )

    async def test_codex_user_input_resume_preserves_selection_and_custom_text(self) -> None:
        params = {
            "threadId": "provider-thread-1",
            "questions": [{
                "id": "approach",
                "question": "Which approach?",
                "options": [
                    {"label": "A", "description": "Use A"},
                    {"label": "B", "description": "Use B"},
                ],
            }],
        }
        detail = _codex_request_detail("item/tool/requestUserInput", params)
        authorization = {
            "requestHash": canonical_json_sha256({
                "target": detail["toolName"],
                "source": detail["source"],
                "serverId": None,
                "arguments": detail["arguments"],
            }),
            "decision": {
                "status": "resolved",
                "payload": {
                    "answers": {
                        "approach": {"selected": ["A"], "custom": "Only recent data"}
                    }
                },
            },
        }
        result = await _handle_server_request(
            method="item/tool/requestUserInput",
            params={**params, "threadId": "provider-thread-2"},
            broker=ApprovalBroker(),
            public_thread_id="thread-1",
            run_id="run-2",
            resume_authorization=authorization,
        )
        self.assertEqual(
            result,
            {"answers": {"approach": {"answers": ["A", "Only recent data"]}}},
        )

    async def test_claude_ask_user_question_uses_durable_answer_contract(self) -> None:
        broker = ApprovalBroker()

        async def runner():
            await asyncio.Event().wait()
            if False:
                yield {}

        stream = broker.stream(runner(), thread_id="thread-q", run_id="run-q1")
        next_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        tool_input = {
            "questions": [{
                "header": "Approach",
                "question": "Which approach?",
                "options": [
                    {"label": "A", "description": "Use A", "preview": "A preview"},
                    {"label": "B", "description": "Use B"},
                ],
                "multiSelect": False,
            }]
        }
        async with ClaudeApprovalBridge(
            broker=broker, thread_id="thread-q", run_id="run-q1"
        ) as bridge:
            config = bridge.child_env()
            reader, writer = await asyncio.open_connection(
                config["K_AGENT_APPROVAL_HOST"], int(config["K_AGENT_APPROVAL_PORT"])
            )
            writer.write((json.dumps({
                "token": config["K_AGENT_APPROVAL_TOKEN"],
                "toolName": "AskUserQuestion",
                "input": tool_input,
            }) + "\n").encode())
            await writer.drain()
            requested = await next_event
            self.assertEqual(requested["payload"]["category"], "user_input")
            self.assertEqual(
                requested["payload"]["detail"]["questions"][0]["id"],
                "question-1",
            )
            self.assertNotIn(
                "preview",
                requested["payload"]["detail"]["questions"][0]["options"][0],
            )
            self.assertEqual((await anext(stream))["type"], "interrupt")
            await reader.readline()
            writer.close()
            await writer.wait_closed()

        authorization = {
            "requestHash": requested["payload"]["requestHash"],
            "decision": {
                "status": "resolved",
                "payload": {
                    "answers": {
                        "question-1": {
                            "selected": ["A"],
                            "custom": "Only recent data",
                        }
                    }
                },
            },
        }
        async with ClaudeApprovalBridge(
            broker=ApprovalBroker(),
            thread_id="thread-q",
            run_id="run-q2",
            resume_authorization=authorization,
        ) as bridge:
            config = bridge.child_env()
            reader, writer = await asyncio.open_connection(
                config["K_AGENT_APPROVAL_HOST"], int(config["K_AGENT_APPROVAL_PORT"])
            )
            writer.write((json.dumps({
                "token": config["K_AGENT_APPROVAL_TOKEN"],
                "toolName": "AskUserQuestion",
                "input": tool_input,
            }) + "\n").encode())
            await writer.drain()
            response = json.loads(await reader.readline())
            self.assertEqual(response["behavior"], "allow")
            self.assertEqual(
                response["updatedInput"]["answers"]["Which approach?"],
                "A, Only recent data",
            )
            self.assertEqual(
                response["updatedInput"]["annotations"]["Which approach?"]["notes"],
                "Only recent data",
            )
            writer.close()
            await writer.wait_closed()

    async def test_claude_permission_bridge_uses_the_same_broker_contract(self) -> None:
        broker = ApprovalBroker()
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
            self.assertEqual((await anext(stream))["type"], "interrupt")
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

        broker = ApprovalBroker()

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
        self.assertEqual((await anext(events)).type, "STATE_SNAPSHOT")
        self.assertEqual((await anext(events)).type, "MESSAGES_SNAPSHOT")
        finished = await anext(events)
        self.assertEqual(finished.type, "RUN_FINISHED")
        self.assertEqual(finished.outcome.type, "interrupt")

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
