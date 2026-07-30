from __future__ import annotations

import json
import unittest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agent.callbacks import AgentRunContext, CallbackManager
from backend.agent.react_agent import OpenAIAgent
from backend.mcp_tool.client import McpClientManager, McpServerConfig, McpSession
from backend.mcp_tool.config import McpTransport, load_scoped_mcp_servers
from backend.permissions import check_permission
from backend.skills import clear_skill_caches, get_available_skills, activate_skills_for_paths
from backend.tools import ToolDefinition
from backend.tools.local import build_skill_tool, invoke_skill
from backend.watchers import PollingChangeWatcher


class McpSkillLoadingTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        clear_skill_caches()

    def test_scoped_mcp_config_deduplicates_by_command_signature(self) -> None:
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".mcp.json").write_text(
                json.dumps({
                    "mcpServers": {
                        "a": {"command": "node", "args": ["server.js"]},
                        "b": {"command": "node", "args": ["server.js"]},
                    }
                }),
                encoding="utf-8",
            )
            result = load_scoped_mcp_servers(cwd)
            self.assertEqual([server.id for server in result.servers], ["a"])
            self.assertEqual(result.suppressed[0]["name"], "b")

    def test_remote_mcp_config_is_normalized(self) -> None:
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://example.test/mcp"}}}),
                encoding="utf-8",
            )
            result = load_scoped_mcp_servers(cwd)
            self.assertEqual(result.servers[0].type, McpTransport.HTTP)
            self.assertEqual(result.servers[0].url, "https://example.test/mcp")

    async def test_mcp_connect_timeout_closes_cold_start_process(self) -> None:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        session = SimpleNamespace(
            session=None,
            connect=AsyncMock(side_effect=wait_forever),
            close=AsyncMock(),
        )
        server = McpServerConfig(
            id="cold-uvx",
            scope="local",
            type="stdio",
            command="uvx",
            args=["example-mcp"],
            env={},
        )
        manager = McpClientManager([server], connect_timeout_seconds=0.01)

        with patch("backend.mcp_tool.client.McpSession", return_value=session):
            await manager.connect_all()

        session.close.assert_awaited_once()
        self.assertIn("cold-uvx", manager.failed)

    async def test_mcp_protocol_error_uses_recoverable_result_contract(self) -> None:
        server = McpServerConfig(
            id="failing-mcp",
            scope="local",
            type="stdio",
            command="test",
            args=[],
            env={},
        )
        mcp_session = McpSession(server)
        content = SimpleNamespace(
            model_dump=lambda mode: {
                "type": "text",
                "text": "remote validation failed",
            }
        )
        mcp_session.session = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(isError=True, content=[content])
            )
        )

        result = json.loads(await mcp_session.call_tool("remote_tool", {}))

        self.assertFalse(result["ok"])
        self.assertEqual(result["tool"], "remote_tool")
        self.assertEqual(result["errorType"], "McpToolError")
        self.assertEqual(result["error"], "remote validation failed")

    def test_skill_dir_frontmatter_and_conditional_activation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_skills = root / "data" / "skill"
            skill_dir = data_skills / "planner"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "description: Plan markdown work\n"
                "paths:\n"
                "- \"**/*.md\"\n"
                "argument-hint: <topic>\n"
                "---\n"
                "Plan $ARGUMENTS from ${K_AGENT_SKILL_DIR}",
                encoding="utf-8",
            )
            with patch("backend.skills.loader.DATA_SKILLS_DIR", data_skills):
                self.assertEqual(get_available_skills(root), [])
                activated = activate_skills_for_paths([root / "notes.md"], root)
                self.assertEqual([skill.id for skill in activated], ["planner"])
                self.assertEqual(get_available_skills(root)[0].argument_hint, "<topic>")

    async def test_skill_tool_expands_content_on_invocation(self) -> None:
        with TemporaryDirectory() as tmp:
            data_skills = Path(tmp) / "data" / "skill"
            skill_dir = data_skills / "remember"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\ndescription: Remember things\narguments: item\n---\nRemember ${item}.",
                encoding="utf-8",
            )
            result = json.loads(
                await invoke_skill(
                    {"skill": "remember", "args": "tea"},
                    [
                        {
                            "id": "remember",
                            "name": "remember",
                            "instructions": "Remember ${item}.",
                            "argumentNames": ["item"],
                            "baseDir": str(skill_dir),
                            "enabled": True,
                        }
                    ],
                )
            )
            self.assertTrue(result["success"])
            self.assertIn("Remember tea.", result["content"])

    def test_only_data_skill_directory_is_loaded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_skills = root / "data" / "skill"
            data_skill = data_skills / "writer"
            project_skill = root / ".k_agent" / "skills" / "ignored"
            user_skill = root / "user-skills" / "ignored-user"
            for directory, name in (
                (data_skill, "写作助手"),
                (project_skill, "项目 Skill"),
                (user_skill, "用户 Skill"),
            ):
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test\n---\nDo work.",
                    encoding="utf-8",
                )
            with patch("backend.skills.loader.DATA_SKILLS_DIR", data_skills):
                clear_skill_caches()
                loaded = get_available_skills(root)
            self.assertEqual([(skill.id, skill.name) for skill in loaded], [("writer", "写作助手")])

    async def test_skill_tool_can_call_mcp_prompt(self) -> None:
        async def call_prompt(server_id: str, prompt_name: str, arguments: dict):
            return json.dumps({"server": server_id, "prompt": prompt_name, "arguments": arguments})

        tool = build_skill_tool(call_prompt)
        result = json.loads(await tool.execute({"skill": "mcp__calendar__create_event", "args": "tomorrow"}))
        self.assertEqual(result["server"], "calendar")
        self.assertEqual(result["prompt"], "create_event")

    async def test_selected_skill_name_is_normalized_to_skill_tool(self) -> None:
        skill = {
            "id": "find-skill-skillhub",
            "name": "find-skill-skillhub",
            "instructions": "Search SkillHub for $ARGUMENTS.",
            "enabled": True,
        }
        agent = OpenAIAgent(
            [build_skill_tool(skills=[skill])],
            McpClientManager([]),
            skills=[skill],
        )

        result = json.loads(
            await agent._run_tool(
                callbacks=CallbackManager(),
                context=AgentRunContext(run_id="test-run"),
                iteration=0,
                tool_name="find-skill-skillhub",
                arguments={"skill": "外卖 点餐 订餐 food delivery"},
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["commandName"], "find-skill-skillhub")
        self.assertIn(
            "Search SkillHub for 外卖 点餐 订餐 food delivery.",
            result["content"],
        )

    async def test_unselected_direct_skill_name_returns_recoverable_error(self) -> None:
        agent = OpenAIAgent(
            [build_skill_tool(skills=[])],
            McpClientManager([]),
            skills=[],
        )

        result = json.loads(
            await agent._run_tool(
                callbacks=CallbackManager(),
                context=AgentRunContext(run_id="test-run"),
                iteration=0,
                tool_name="find-skill-skillhub",
                arguments={"skill": "外卖"},
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["tool"], "find-skill-skillhub")
        self.assertEqual(result["errorType"], "RuntimeError")
        self.assertEqual(
            result["error"],
            "Unknown tool requested: find-skill-skillhub",
        )

    async def test_local_tool_exception_returns_reason_for_model_retry(self) -> None:
        async def fail(_: dict) -> str:
            raise ValueError("path is outside workspace: /tmp/result.md")

        agent = OpenAIAgent(
            [
                ToolDefinition(
                    name="Read",
                    description="Read a file.",
                    parameters={
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                        "additionalProperties": False,
                    },
                    execute=fail,
                )
            ],
            McpClientManager([]),
        )

        result = json.loads(
            await agent._run_tool(
                callbacks=CallbackManager(),
                context=AgentRunContext(run_id="test-run"),
                iteration=0,
                tool_name="Read",
                arguments={"file_path": "/tmp/result.md"},
            )
        )

        self.assertEqual(
            result,
            {
                "ok": False,
                "tool": "Read",
                "error": "path is outside workspace: /tmp/result.md",
                "errorType": "ValueError",
            },
        )

    def test_malformed_tool_arguments_are_recoverable(self) -> None:
        agent = OpenAIAgent([], McpClientManager([]))

        with self.assertRaisesRegex(ValueError, "JSON object"):
            agent._decode_tool_arguments("[1, 2]")

    def test_permission_rules_deny(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {"K_AGENT_PERMISSION_RULES": str(Path(tmp) / "permissions.json")}, clear=False):
            Path(tmp, "permissions.json").write_text(
                json.dumps({"rules": [{"tool": "Skill", "pattern": "danger", "behavior": "deny"}]}),
                encoding="utf-8",
            )
            decision = check_permission("Skill", "danger")
            self.assertEqual(decision.behavior, "deny")

    async def test_polling_watcher_detects_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            changed = asyncio.Event()
            root = Path(tmp)
            watched = root / "CLAUDE.md"
            watched.write_text("initial", encoding="utf-8")
            watcher = PollingChangeWatcher([root], lambda _: changed.set(), interval_seconds=0.05)
            watcher.start()
            await asyncio.sleep(0.1)
            watched.write_text("updated", encoding="utf-8")
            await asyncio.wait_for(changed.wait(), timeout=1)
            await watcher.stop()

if __name__ == "__main__":
    unittest.main()
