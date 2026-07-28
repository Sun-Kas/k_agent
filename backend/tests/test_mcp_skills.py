from __future__ import annotations

import json
import unittest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.mcp_tool.config import McpTransport, load_scoped_mcp_servers
from backend.permissions import check_permission
from access_layer.sessions.store import SessionStore
from backend.skills import clear_skill_caches, get_available_skills, activate_skills_for_paths
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

    def test_skill_dir_frontmatter_and_conditional_activation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / ".k_agent" / "skills" / "planner"
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
            self.assertEqual(get_available_skills(root), [])
            activated = activate_skills_for_paths([root / "notes.md"], root)
            self.assertEqual([skill.name for skill in activated], ["planner"])
            self.assertEqual(get_available_skills(root)[0].argument_hint, "<topic>")

    async def test_skill_tool_expands_content_on_invocation(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {"K_AGENT_SKILLS_DIR": str(Path(tmp) / "skills")}, clear=False):
            skill_dir = Path(tmp) / "skills" / "remember"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\ndescription: Remember things\narguments: item\n---\nRemember ${item}.",
                encoding="utf-8",
            )
            clear_skill_caches()
            result = json.loads(await invoke_skill({"skill": "remember", "args": "tea"}))
            self.assertTrue(result["success"])
            self.assertIn("Remember tea.", result["content"])

    async def test_skill_tool_can_call_mcp_prompt(self) -> None:
        async def call_prompt(server_id: str, prompt_name: str, arguments: dict):
            return json.dumps({"server": server_id, "prompt": prompt_name, "arguments": arguments})

        tool = build_skill_tool(call_prompt)
        result = json.loads(await tool.execute({"skill": "mcp__calendar__create_event", "args": "tomorrow"}))
        self.assertEqual(result["server"], "calendar")
        self.assertEqual(result["prompt"], "create_event")

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
            watched = root / "K_AGENT.md"
            watched.write_text("initial", encoding="utf-8")
            watcher = PollingChangeWatcher([root], lambda _: changed.set(), interval_seconds=0.05)
            watcher.start()
            await asyncio.sleep(0.1)
            watched.write_text("updated", encoding="utf-8")
            await asyncio.wait_for(changed.wait(), timeout=1)
            await watcher.stop()

    def test_session_load_keeps_thinking_boundaries_and_removes_tool_steps(self) -> None:
        payload = {
            "id": "session-1",
            "title": "legacy",
            "messages": [
                {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": "done",
                    "createdAt": "2026-07-28T12:00:00+00:00",
                    "meta": {
                        "thinkingGroups": [
                            {
                                "id": "g1",
                                "steps": [{"id": "s1", "phase": "reasoning", "title": "分析并决定下一步"}],
                                "closed": True,
                                "textStart": 0,
                                "textEnd": 0,
                            },
                            {
                                "id": "g2",
                                "steps": [{"id": "s2", "phase": "tool", "title": "调用 append_personal_memory"}],
                                "closed": True,
                                "textStart": 8,
                                "textEnd": 8,
                            },
                        ]
                    },
                }
            ],
            "thinkingGroups": [
                {
                    "id": "g1",
                    "steps": [{"id": "s1", "phase": "reasoning", "title": "分析并决定下一步"}],
                    "closed": True,
                    "textStart": 0,
                    "textEnd": 0,
                },
                {
                    "id": "g2",
                    "steps": [{"id": "s2", "phase": "tool", "title": "调用 append_personal_memory"}],
                    "closed": True,
                    "textStart": 8,
                    "textEnd": 8,
                },
            ],
            "updatedAt": "2026-07-28T12:00:00+00:00",
        }

        record = SessionStore._record_from_payload(payload)
        self.assertEqual(len(record.thinking_groups), 1)
        self.assertEqual(len(record.messages[0].meta.thinking_groups), 1)
        self.assertEqual([step["id"] for step in record.messages[0].meta.thinking_groups[0]["steps"]], ["s1"])


if __name__ == "__main__":
    unittest.main()
