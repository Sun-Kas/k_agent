from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.agent.callbacks import AgentRunContext, CallbackManager
from backend.agent.react_agent import OpenAIAgent
from backend.permissions import check_permissions, default_behavior
from backend.permissions import rules as permission_rules
from backend.tools import ToolDefinition
from backend.tools.cc_extra import _html_to_text, _is_public_address, cc_web_fetch
from backend.tools.workspace import reset_tool_network_access, set_tool_network_access


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

    async def test_ask_is_refused_with_an_actionable_message(self) -> None:
        async def execute(_: dict) -> str:
            return "ran"

        tool = ToolDefinition(
            name="Bash",
            description="",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )
        agent = OpenAIAgent([tool], mcp_client_manager=None)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "permissions.json"
            path.write_text(
                json.dumps({"rules": [{"tool": "Bash", "pattern": "*", "behavior": "ask"}]}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"K_AGENT_PERMISSION_RULES": str(path)}):
                result = await agent._run_tool(
                    callbacks=CallbackManager([]),
                    context=AgentRunContext(),
                    iteration=0,
                    tool_name="Bash",
                    arguments={"command": "ls"},
                )
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("requires manual approval", payload["error"])

    async def test_default_escalation_uses_hitl_before_local_tool_runs(self) -> None:
        calls: list[str] = []

        async def execute(_: dict) -> str:
            calls.append("tool")
            return "ran"

        async def approve(target, decision, detail):
            calls.append(f"approval:{target}:{decision.behavior}")
            self.assertEqual(detail["arguments"]["sandbox_permissions"], "require_escalated")
            return {"action": "approve", "remember": False}

        agent = OpenAIAgent(
            [ToolDefinition("Read", "", {"type": "object", "properties": {}}, execute)],
            mcp_client_manager=None,
            approval_handler=approve,
        )
        context = AgentRunContext()
        context.metadata["permission_mode"] = "default"
        result = await agent._run_tool(
            callbacks=CallbackManager([]), context=context, iteration=0,
            tool_name="Read",
            arguments={"file_path": "/outside/file", "sandbox_permissions": "require_escalated"},
        )

        self.assertEqual(result, "ran")
        self.assertEqual(calls, ["approval:Read:ask", "tool"])

    async def test_full_access_bypasses_permission_rules(self) -> None:
        async def execute(_: dict) -> str:
            return "ran"

        agent = OpenAIAgent(
            [ToolDefinition("Bash", "", {"type": "object", "properties": {}}, execute)],
            mcp_client_manager=None,
        )
        context = AgentRunContext()
        context.metadata["permission_mode"] = "full_access"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "permissions.json"
            path.write_text(json.dumps({"rules": [{"tool": "Bash", "pattern": "*", "behavior": "deny"}]}), encoding="utf-8")
            with patch.dict(os.environ, {"K_AGENT_PERMISSION_RULES": str(path)}):
                result = await agent._run_tool(
                    callbacks=CallbackManager([]), context=context, iteration=0,
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

        agent = OpenAIAgent(
            [
                ToolDefinition("Skill", "", {"type": "object", "properties": {}}, skill_execute),
                ToolDefinition("Bash", "", {"type": "object", "properties": {}}, bash_execute),
            ],
            mcp_client_manager=None,
        )
        context = AgentRunContext()
        callbacks = CallbackManager([])
        await agent._run_tool(
            callbacks=callbacks,
            context=context,
            iteration=0,
            tool_name="Skill",
            arguments={"skill": "reviewer"},
        )
        self.assertEqual(context.skill_allowlist, {"Read", "Skill"})
        blocked = json.loads(
            await agent._run_tool(
                callbacks=callbacks,
                context=context,
                iteration=1,
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
