from __future__ import annotations

import json
import unittest
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agent.contracts import AgentRunRequest
from backend.agent.react_agent import OpenAIAgent
from backend.api.schemas import ChatMessage
from backend.mcp_tool.client import McpClientManager, McpServerConfig, McpSession
from backend.mcp_tool.config import McpTransport, load_scoped_mcp_servers
from backend.permissions import check_permission
from backend.tools import ToolDefinition
from backend.tools.local import build_skill_tool, invoke_skill
from backend.watchers import PollingChangeWatcher


def _agent_request() -> AgentRunRequest:
    return AgentRunRequest(
        messages=[
            ChatMessage(
                id="skill-test-user",
                role="user",
                content="test",
                createdAt=datetime.now(timezone.utc),
            )
        ],
        system_prompt="test",
        user_context={},
        model_config={"model": "test", "apiKey": "test"},
    )


class McpSkillLoadingTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_skill_tool_expands_content_on_invocation(self) -> None:
        with TemporaryDirectory() as tmp:
            data_skills = Path(tmp) / "data" / "skill"
            skill_dir = data_skills / "remember"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\ndescription: Remember things\narguments: item\n---\nRemember ${item}.",
                encoding="utf-8",
            )
            with patch("backend.skills.body.DATA_SKILLS_DIR", data_skills):
                result = json.loads(
                    await invoke_skill(
                        {"skill": "remember", "args": "tea"},
                        [
                            {
                                "id": "remember",
                                "name": "remember",
                                "argumentNames": ["item"],
                                "enabled": True,
                            }
                        ],
                    )
                )
            self.assertTrue(result["success"])
            self.assertIn("Remember tea.", result["content"])
            self.assertEqual(result["baseDir"], str(skill_dir.resolve()))
            self.assertIn("Skill package root:", result["content"])

    async def test_skill_tool_reads_body_but_keeps_request_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            data_skills = Path(tmp) / "skills"
            skill_dir = data_skills / "remember"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\narguments: ignored-from-file\n---\nRemember ${item}.",
                encoding="utf-8",
            )
            with patch("backend.skills.body.DATA_SKILLS_DIR", data_skills):
                result = json.loads(
                    await invoke_skill(
                        {"skill": "remember", "args": "tea"},
                        [
                            {
                                "id": "remember",
                                "name": "remember",
                                "enabled": True,
                                "argumentNames": ["item"],
                            }
                        ],
                    )
                )
            self.assertTrue(result["success"])
            self.assertIn("Remember tea.", result["content"])
            self.assertNotIn("ignored-from-file", result["content"])

    async def test_skill_tool_does_not_read_body_before_authorization(self) -> None:
        with patch("backend.tools.local.load_skill_body") as loader:
            unknown = json.loads(
                await invoke_skill({"skill": "unknown", "args": ""}, [])
            )
            disabled = json.loads(
                await invoke_skill(
                    {"skill": "disabled", "args": ""},
                    [
                        {
                            "id": "disabled",
                            "name": "disabled",
                            "enabled": True,
                            "disableModelInvocation": True,
                        }
                    ],
                )
            )

        loader.assert_not_called()
        self.assertFalse(unknown["success"])
        self.assertFalse(disabled["success"])

    async def test_skill_tool_rejects_unsafe_catalog_id(self) -> None:
        result = json.loads(
            await invoke_skill(
                {"skill": "escape", "args": ""},
                [{"id": "../escape", "name": "escape", "enabled": True}],
            )
        )

        self.assertFalse(result["success"])
        self.assertIn("Invalid Skill id", result["error"])

    async def test_skill_tool_expands_community_skill_dir_placeholders(self) -> None:
        with TemporaryDirectory() as tmp:
            data_skills = Path(tmp) / "skills"
            skill_dir = data_skills / "steam-daily-deals"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "Run:\n"
                "python3 {SKILL_DIR}/scripts/fetch_steam_deals.py\n"
                "Also ${SKILL_DIR}/refs and $SKILL_DIR/assets",
                encoding="utf-8",
            )
            with patch("backend.skills.body.DATA_SKILLS_DIR", data_skills):
                result = json.loads(
                    await invoke_skill(
                        {"skill": "steam-daily-deals", "args": "今天的Steam优惠"},
                        [
                            {
                                "id": "steam-daily-deals",
                                "name": "steam-daily-deals",
                                "argumentNames": [],
                                "enabled": True,
                            }
                        ],
                    )
                )
            self.assertTrue(result["success"])
            resolved_skill_dir = skill_dir.resolve()
            self.assertEqual(result["baseDir"], str(resolved_skill_dir))
            self.assertEqual(result["filePath"], str(resolved_skill_dir / "SKILL.md"))
            self.assertNotIn("{SKILL_DIR}", result["content"])
            self.assertNotIn("${SKILL_DIR}", result["content"])
            self.assertNotIn("$SKILL_DIR", result["content"])
            self.assertIn(
                f"python3 {resolved_skill_dir}/scripts/fetch_steam_deals.py",
                result["content"],
            )
            self.assertIn(f"{resolved_skill_dir}/refs", result["content"])
            self.assertIn(f"{resolved_skill_dir}/assets", result["content"])

    async def test_skill_tool_rewrites_tmp_outputs_into_session_workspace(self) -> None:
        from backend.tools.workspace import reset_tool_workspace, set_tool_workspace

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_skills = root / "skills"
            skill_dir = data_skills / "steam-daily-deals"
            workspace = root / "workspace"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "python3 {SKILL_DIR}/scripts/fetch.py "
                "--output-file /tmp/steam_deals_today.md\n"
                "Read /tmp/steam_deals_today.md",
                encoding="utf-8",
            )
            workspace.mkdir()
            token = set_tool_workspace(workspace)
            try:
                with patch("backend.skills.body.DATA_SKILLS_DIR", data_skills):
                    result = json.loads(
                        await invoke_skill(
                            {"skill": "steam-daily-deals", "args": ""},
                            [
                                {
                                    "id": "steam-daily-deals",
                                    "name": "steam-daily-deals",
                                    "argumentNames": [],
                                    "enabled": True,
                                }
                            ],
                        )
                    )
            finally:
                reset_tool_workspace(token)
            self.assertTrue(result["success"])
            self.assertNotIn("/tmp/", result["content"])
            resolved_workspace = str(workspace.resolve())
            self.assertIn(f"{resolved_workspace}/steam_deals_today.md", result["content"])
            self.assertIn(
                f"Session workspace (write outputs here): {resolved_workspace}",
                result["content"],
            )

    async def test_backend_body_wins_but_request_metadata_controls_arguments(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_skills = root / "skills"
            skill_dir = data_skills / "writer"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: disk-version\narguments: disk-argument\n---\nDISK BODY ${topic}.",
                encoding="utf-8",
            )
            with patch("backend.skills.body.DATA_SKILLS_DIR", data_skills):
                result = json.loads(
                    await invoke_skill(
                        {"skill": "writer", "args": "report"},
                        [
                            {
                                "id": "writer",
                                "name": "writer",
                                "enabled": True,
                                # 旧客户端即使夹带正文或路径，Backend 也不能信任。
                                "instructions": "REQUEST BODY MUST NOT WIN.",
                                "argumentNames": ["topic"],
                                "filePath": "/poison/SKILL.md",
                                "baseDir": "/poison",
                            }
                        ],
                    )
                )
            self.assertTrue(result["success"])
            self.assertIn("DISK BODY report.", result["content"])
            self.assertNotIn("REQUEST BODY MUST NOT WIN", result["content"])
            self.assertNotIn("disk-argument", result["content"])
            self.assertEqual(result["filePath"], str(skill_dir.resolve() / "SKILL.md"))

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
            "enabled": True,
        }
        with TemporaryDirectory() as tmp:
            data_skills = Path(tmp) / "skills"
            skill_dir = data_skills / "find-skill-skillhub"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "Search SkillHub for $ARGUMENTS.", encoding="utf-8"
            )
            agent = OpenAIAgent()
            runtime = await agent.create_runtime(
                _agent_request(),
                [build_skill_tool(skills=[skill])],
                McpClientManager([]),
                skills=[skill],
            )

            with patch("backend.skills.body.DATA_SKILLS_DIR", data_skills):
                result = json.loads(
                    await agent._run_tool(
                        runtime=runtime,
                        iteration=0,
                        call_id="call-skill",
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
        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
            [build_skill_tool(skills=[])],
            McpClientManager([]),
            skills=[],
        )

        with patch("backend.tools.local.load_skill_body") as loader:
            result = json.loads(
                await agent._run_tool(
                    runtime=runtime,
                    iteration=0,
                    call_id="call-unknown",
                    tool_name="find-skill-skillhub",
                    arguments={"skill": "外卖"},
                )
            )
        loader.assert_not_called()

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

        agent = OpenAIAgent()
        runtime = await agent.create_runtime(
            _agent_request(),
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
                runtime=runtime,
                iteration=0,
                call_id="call-read",
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
        agent = OpenAIAgent()

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
