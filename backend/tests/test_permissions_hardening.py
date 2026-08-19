from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.agent.contracts import AgentRunRequest
from backend.agent.react_agent import OpenAIAgent
from backend.api.schemas import ChatMessage
from backend.mcp_tool import McpClientManager
from backend.permissions import check_permissions, default_behavior
from backend.permissions import rules as permission_rules
from backend.tools import ToolDefinition
from backend.tools.cc_like import cc_glob, cc_grep, cc_read
from backend.tools.cc_extra import _html_to_text, _is_public_address, cc_ls, cc_web_fetch
from backend.tools.workspace import (
    reset_tool_network_access,
    reset_tool_workspace,
    set_tool_network_access,
    set_tool_workspace,
)


def _agent_request() -> AgentRunRequest:
    """Minimal request used to exercise request-scoped OpenAIAgent runtimes."""

    return AgentRunRequest(
        messages=[
            ChatMessage(
                id="permission-test-user",
                role="user",
                content="test",
                createdAt=datetime.now(timezone.utc),
            )
        ],
        system_prompt="test",
        user_context={},
        model_config={"model": "test", "apiKey": "test"},
    )


class PermissionRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        permission_rules._RULE_CACHE = None

    def tearDown(self) -> None:
        permission_rules._RULE_CACHE = None

    def write_rules(self, directory: str, rules: list[dict[str, str]]) -> str:
        path = Path(directory) / "permissions.json"
        path.write_text(json.dumps({"rules": rules}), encoding="utf-8")
        return str(path)

    def test_chained_shell_command_cannot_bypass_a_deny_rule(self) -> None:
        with TemporaryDirectory() as tmp:
            path = self.write_rules(tmp, [{"tool": "Bash", "pattern": "rm *", "behavior": "deny"}])
            with patch.dict(os.environ, {"K_AGENT_PERMISSION_RULES": path}):
                subjects = OpenAIAgent._permission_subjects(
                    "Bash", {"command": "cd /tmp && rm -rf build"}
                )
                decision = check_permissions("Bash", subjects)
        self.assertEqual(decision.behavior, "deny")

    def test_unrelated_chained_command_stays_allowed(self) -> None:
        with TemporaryDirectory() as tmp:
            path = self.write_rules(tmp, [{"tool": "Bash", "pattern": "rm *", "behavior": "deny"}])
            with patch.dict(os.environ, {"K_AGENT_PERMISSION_RULES": path}):
                subjects = OpenAIAgent._permission_subjects(
                    "Bash", {"command": "cd /tmp && ls -la"}
                )
                decision = check_permissions("Bash", subjects)
        self.assertEqual(decision.behavior, "allow")

    def test_default_policy_can_be_switched_to_deny(self) -> None:
        with patch.dict(os.environ, {"K_AGENT_PERMISSION_DEFAULT": "deny"}):
            self.assertEqual(default_behavior(), "deny")
            decision = check_permissions("Bash", ["echo hi"])
        self.assertEqual(decision.behavior, "deny")
        self.assertIn("default policy is deny", decision.reason or "")

    def test_edited_rule_file_is_picked_up_without_a_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            path = self.write_rules(tmp, [{"tool": "Bash", "pattern": "*", "behavior": "allow"}])
            with patch.dict(os.environ, {"K_AGENT_PERMISSION_RULES": path}):
                self.assertEqual(check_permissions("Bash", ["ls"]).behavior, "allow")
                Path(path).write_text(
                    json.dumps({"rules": [{"tool": "Bash", "pattern": "*", "behavior": "deny"}]}),
                    encoding="utf-8",
                )
                os.utime(path, (0, 0))
                self.assertEqual(check_permissions("Bash", ["ls"]).behavior, "deny")


class AskBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        permission_rules._RULE_CACHE = None

    def tearDown(self) -> None:
        permission_rules._RULE_CACHE = None

    async def test_single_agent_creates_isolated_request_runtimes(self) -> None:
        agent = OpenAIAgent()
        first = await agent.create_runtime(
            _agent_request(), [], McpClientManager([])
        )
        second = await agent.create_runtime(
            _agent_request(), [], McpClientManager([])
        )

        self.assertIsInstance(first, dict)
        self.assertFalse(hasattr(agent, "__dict__"))
        first["approved_targets"].add("Bash")

        self.assertIsNot(first, second)
        self.assertEqual(first["approved_targets"], {"Bash"})
        self.assertEqual(second["approved_targets"], set())

    async def test_ask_is_refused_with_an_actionable_message(self) -> None:
        async def execute(_: dict) -> str:
            return "ran"

        tool = ToolDefinition(
            name="Bash",
            description="",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )
        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(), [tool], McpClientManager([])
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "permissions.json"
            path.write_text(
                json.dumps({"rules": [{"tool": "Bash", "pattern": "*", "behavior": "ask"}]}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"K_AGENT_PERMISSION_RULES": str(path)}):
                result = await agent._run_tool(
                    runtime=runtime,
                    iteration=0,
                    call_id="call-bash",
                    tool_name="Bash",
                    arguments={"command": "ls"},
                )
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("requires manual approval", payload["error"])

    async def test_read_ignores_stale_escalation_without_hitl(self) -> None:
        calls: list[str] = []

        async def execute(_: dict) -> str:
            calls.append("tool")
            return "ran"

        async def approve(*_args):
            calls.append("approval")
            return {"action": "approve", "scope": "once"}

        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [ToolDefinition("Read", "", {"type": "object", "properties": {}}, execute)],
            McpClientManager([]),
            approval_handler=approve,
        )
        result = await agent._run_tool(
            runtime=runtime,
            iteration=0, call_id="call-read",
            tool_name="Read",
            arguments={"file_path": "/outside/file", "sandbox_permissions": "require_escalated"},
        )

        self.assertEqual(result, "ran")
        self.assertEqual(calls, ["tool"])

    async def test_read_only_tools_can_access_paths_outside_session_workspace(self) -> None:
        with TemporaryDirectory() as session_dir, TemporaryDirectory() as project_dir:
            project = Path(project_dir)
            report = project / "report.html"
            report.write_text("permission-boundary-marker", encoding="utf-8")
            token = set_tool_workspace(Path(session_dir))
            try:
                read_result = json.loads(await cc_read({"file_path": str(report)}))
                ls_result = json.loads(await cc_ls({"path": str(project)}))
                glob_result = json.loads(await cc_glob({"path": str(project), "pattern": "*.html"}))
                grep_result = json.loads(await cc_grep({"path": str(project), "pattern": "boundary-marker"}))
            finally:
                reset_tool_workspace(token)

        self.assertTrue(read_result["ok"])
        self.assertEqual(read_result["content"], "permission-boundary-marker")
        resolved_report = str(report.resolve())
        self.assertEqual([item["path"] for item in ls_result["entries"]], [resolved_report])
        self.assertEqual(glob_result["matches"], [resolved_report])
        self.assertEqual(len(grep_result["matches"]), 1)

    async def test_write_escalation_still_uses_hitl(self) -> None:
        calls: list[str] = []

        async def execute(_: dict) -> str:
            calls.append("tool")
            return "ran"

        async def approve(_target, decision, _detail):
            calls.append(f"approval:{decision.behavior}")
            return {"action": "approve", "scope": "once"}

        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [ToolDefinition("Write", "", {"type": "object", "properties": {}}, execute)],
            McpClientManager([]),
            approval_handler=approve,
        )
        result = await agent._run_tool(
            runtime=runtime,
            iteration=0,
            call_id="call-write",
            tool_name="Write",
            arguments={"file_path": "/outside/file", "sandbox_permissions": "require_escalated"},
        )

        self.assertEqual(result, "ran")
        self.assertEqual(calls, ["approval:ask", "tool"])

    async def test_unstructured_bash_escalation_is_denied_before_hitl(self) -> None:
        calls: list[str] = []

        async def execute(_: dict) -> str:
            calls.append("tool")
            return "ran"

        async def approve(*_args):
            calls.append("approval")
            return {"action": "approve"}

        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [ToolDefinition("Bash", "", {"type": "object", "properties": {}}, execute)],
            McpClientManager([]),
            approval_handler=approve,
        )
        result = await agent._run_tool(
            runtime=runtime,
            iteration=0, call_id="call-bash",
            tool_name="Bash",
            arguments={
                "command": "curl https://store.steampowered.com",
                "description": "required network destination",
                "sandbox_permissions": "require_escalated",
            },
        )

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("requires escalation_scope", payload["error"])
        self.assertEqual(calls, [])

    async def test_allowlisted_network_escalation_is_denied_before_hitl(self) -> None:
        calls: list[str] = []

        async def execute(_: dict) -> str:
            calls.append("tool")
            return "ran"

        async def approve(*_args):
            calls.append("approval")
            return {"action": "approve"}

        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [ToolDefinition("Bash", "", {"type": "object", "properties": {}}, execute)],
            McpClientManager([]),
            approval_handler=approve,
        )
        result = await agent._run_tool(
            runtime=runtime,
            iteration=0, call_id="call-bash",
            tool_name="Bash",
            arguments={
                "command": "curl https://store.steampowered.com",
                "sandbox_permissions": "require_escalated",
                "escalation_scope": "network_destination",
                "escalation_resource": "store.steampowered.com",
            },
        )

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("already allowed", payload["error"])
        self.assertEqual(calls, [])

    async def test_non_allowlisted_network_destination_uses_hitl(self) -> None:
        calls: list[str] = []

        async def execute(_: dict) -> str:
            calls.append("tool")
            return "ran"

        async def approve(_target, decision, _detail):
            calls.append(f"approval:{decision.behavior}")
            return {"action": "approve", "scope": "once"}

        parameters = {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "sandbox_permissions": {"type": "string"},
                "escalation_scope": {"type": "string"},
                "escalation_resource": {"type": "string"},
            },
            "required": ["command"],
            "additionalProperties": False,
        }
        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [ToolDefinition("Bash", "", parameters, execute)],
            McpClientManager([]),
            approval_handler=approve,
        )
        result = await agent._run_tool(
            runtime=runtime,
            iteration=0, call_id="call-bash",
            tool_name="Bash",
            arguments={
                "command": "curl https://outside.example",
                "sandbox_permissions": "require_escalated",
                "escalation_scope": "network_destination",
                "escalation_resource": "outside.example",
            },
        )

        self.assertEqual(result, "ran")
        self.assertEqual(calls, ["approval:ask", "tool"])

    async def test_structured_bash_local_resource_escalation_uses_hitl(self) -> None:
        calls: list[str] = []

        async def execute(_: dict) -> str:
            calls.append("tool")
            return "ran"

        async def approve(_target, decision, _detail):
            calls.append(f"approval:{decision.behavior}")
            return {"action": "approve", "scope": "once"}

        parameters = {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "sandbox_permissions": {"type": "string"},
                "escalation_scope": {"type": "string"},
                "escalation_resource": {"type": "string"},
            },
            "required": ["command"],
            "additionalProperties": False,
        }
        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [ToolDefinition("Bash", "", parameters, execute)],
            McpClientManager([]),
            approval_handler=approve,
        )
        result = await agent._run_tool(
            runtime=runtime,
            iteration=0, call_id="call-bash",
            tool_name="Bash",
            arguments={
                "command": "touch /etc/example",
                "sandbox_permissions": "require_escalated",
                "escalation_scope": "outside_workspace_write",
                "escalation_resource": "/etc/example",
            },
        )

        self.assertEqual(result, "ran")
        self.assertEqual(calls, ["approval:ask", "tool"])

    async def test_full_access_bypasses_permission_rules(self) -> None:
        async def execute(_: dict) -> str:
            return "ran"

        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [ToolDefinition("Bash", "", {"type": "object", "properties": {}}, execute)],
            McpClientManager([]),
        )
        runtime["pipeline"].context.metadata["permission_mode"] = "full_access"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "permissions.json"
            path.write_text(json.dumps({"rules": [{"tool": "Bash", "pattern": "*", "behavior": "deny"}]}), encoding="utf-8")
            with patch.dict(os.environ, {"K_AGENT_PERMISSION_RULES": str(path)}):
                result = await agent._run_tool(
                    runtime=runtime,
                    iteration=0, call_id="call-bash",
                    tool_name="Bash", arguments={"command": "whoami"},
                )
        self.assertEqual(result, "ran")


class SkillAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoked_skill_restricts_later_tools_in_the_run(self) -> None:
        async def skill_execute(_: dict) -> str:
            return json.dumps({
                "success": True,
                "commandName": "reviewer",
                "allowedTools": ["Read"],
                "content": "review the diff",
            })

        async def bash_execute(_: dict) -> str:
            return "ran"

        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [
                ToolDefinition("Skill", "", {"type": "object", "properties": {}}, skill_execute),
                ToolDefinition("Bash", "", {"type": "object", "properties": {}}, bash_execute),
            ],
            McpClientManager([]),
        )
        await agent._run_tool(
            runtime=runtime,
            iteration=0,
            call_id="call-skill",
            tool_name="Skill",
            arguments={"skill": "reviewer"},
        )
        self.assertEqual(
            runtime["pipeline"].context.skill_allowlist,
            {"Read", "Skill"},
        )
        blocked = json.loads(
            await agent._run_tool(
                runtime=runtime,
                iteration=1,
                call_id="call-bash",
                tool_name="Bash",
                arguments={"command": "ls"},
            )
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("restricts tool use to", blocked["error"])


class WebFetchGuardTests(unittest.TestCase):
    def test_run_network_policy_returns_a_recoverable_tool_error(self) -> None:
        token = set_tool_network_access(False)
        try:
            payload = json.loads(
                asyncio.run(cc_web_fetch({"url": "https://example.com"}))
            )
        finally:
            reset_tool_network_access(token)
        self.assertFalse(payload["ok"])
        self.assertIn("disabled", payload["error"])

    def test_loopback_and_private_hosts_are_not_public(self) -> None:
        self.assertFalse(_is_public_address("127.0.0.1"))
        self.assertFalse(_is_public_address("localhost"))
        self.assertFalse(_is_public_address("10.0.0.1"))
        self.assertFalse(_is_public_address("169.254.169.254"))

    def test_html_cleanup_no_longer_eats_letters(self) -> None:
        self.assertEqual(_html_to_text("<p>traffic</p>"), "traffic")
        self.assertEqual(_html_to_text("<script>var x=1</script>keep"), "keep")


if __name__ == "__main__":
    unittest.main()
