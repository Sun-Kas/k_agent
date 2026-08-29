from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.runners.cli_models import list_models_for_kind
from backend.runners.claude_code import (
    ClaudeCodeRunner,
    ClaudeCodeStreamState,
    _claude_allowed_tools,
    build_claude_skill_preamble,
    claude_sandbox_settings,
    map_claude_event,
)
from backend.runners.claude_approval_mcp import server as claude_approval_mcp
from backend.runners.claude_mcp import (
    inject_claude_mcp_secrets,
    write_claude_mcp_config,
)
from backend.runners.cli_process import (
    build_cli_prompt,
    build_prompt_from_messages,
    extract_latest_user_prompt,
)
from backend.runners.base import RunnerContext
from backend.approvals import ApprovalBroker
from backend.runners.codex import CodexRunner, build_codex_skill_preamble, map_codex_event
from backend.runners.codex_app_server import CodexStreamState
from backend.runners.network_policy import network_access_enabled
from backend.runners.codex_config import write_codex_mcp_config
from backend.runners.detect import detect_agents
from backend.runners.registry import RunnerRegistry, get_default_registry
from backend.api.schemas import ChatMessage
from datetime import datetime, timezone


class RunnerRegistryTests(unittest.TestCase):
    def test_default_registry_includes_builtin_and_cli_kinds(self) -> None:
        registry = get_default_registry()
        self.assertIs(get_default_registry(), registry)
        self.assertEqual(registry.kinds(), ["claude_code", "codex", "k_agent"])
        k_agent = registry.get(None)
        self.assertEqual(k_agent.kind, "k_agent")
        self.assertIs(registry.get("k_agent"), k_agent)
        codex = registry.get("codex")
        self.assertEqual(codex.kind, "codex")
        self.assertIs(registry.get("codex"), codex)
        with self.assertRaises(ValueError):
            registry.get("unknown_agent")

    def test_registry_loads_once_on_first_get_and_rejects_overwrite(self) -> None:
        calls: list[str] = []

        class TestRunner:
            kind = "test"

            async def run_stream(self, runtime):
                if False:
                    yield runtime

        def load_test_runner():
            calls.append("loaded")
            return TestRunner()

        registry = RunnerRegistry()
        registry.register("test", load_test_runner)

        self.assertEqual(registry.kinds(), ["test"])
        self.assertEqual(calls, [])
        first = registry.get("test")
        self.assertIs(registry.get("test"), first)
        self.assertEqual(calls, ["loaded"])
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register("test", load_test_runner)

    def test_singleton_runners_expose_runtime_builder(self) -> None:
        registry = get_default_registry()

        for kind in registry.kinds():
            with self.subTest(kind=kind):
                runner = registry.get(kind)
                self.assertIs(registry.get(kind), runner)
                self.assertTrue(callable(getattr(runner, "create_runtime", None)))


class ClaudeApprovalMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_prompt_tool_has_claude_contract_fields(self) -> None:
        tools = await claude_approval_mcp.list_tools()
        approval = next(tool for tool in tools if tool.name == "request_approval")
        self.assertEqual(
            set(approval.inputSchema["required"]),
            {"tool_name", "input"},
        )
        self.assertIsNone(approval.outputSchema)


class CliModelsTests(unittest.TestCase):
    def test_claude_models_include_aliases(self) -> None:
        catalog = list_models_for_kind("claude_code")
        ids = {model["id"] for model in catalog["models"]}
        self.assertIn("sonnet", ids)
        self.assertIn("opus", ids)
        self.assertEqual(catalog["defaultModelId"], "sonnet")

    def test_codex_models_non_empty(self) -> None:
        catalog = list_models_for_kind("codex")
        self.assertTrue(catalog["models"])
        self.assertTrue(catalog["defaultModelId"])


class CliRunnerBoundaryTests(unittest.TestCase):
    def test_shared_cli_process_has_no_provider_specific_logic(self) -> None:
        """Keep provider configuration and stream state out of the shared runner."""

        shared_runner = (
            Path(__file__).resolve().parents[1] / "runners" / "cli_process.py"
        ).read_text(encoding="utf-8")
        for provider_marker in (
            "Claude",
            "Codex",
            "claude_mcp",
            "codex_config",
            "mcp_stdio_proxy",
        ):
            with self.subTest(provider_marker=provider_marker):
                self.assertNotIn(provider_marker, shared_runner)

    def test_voice_conversation_styles_cli_prompt_without_mutating_message(self) -> None:
        message = ChatMessage(
            id="message-voice",
            role="user",
            content="介绍一下这个功能",
            createdAt=datetime.now(timezone.utc),
        )
        ctx = RunnerContext(
            thread_id="thread-voice",
            run_id="run-voice",
            request_id="request-voice",
            messages=[message],
            model_id=None,
            mcp_servers=[],
            skills=[],
            reasoning_effort=None,
            attachments=[],
            options={"voiceConversation": True},
        )

        prompt = build_cli_prompt(ctx)

        self.assertIn("Voice conversation response style", prompt)
        self.assertIn("介绍一下这个功能", prompt)
        self.assertEqual(message.content, "介绍一下这个功能")


class NetworkPolicyTests(unittest.TestCase):
    def _context(self, *, options=None, settings=None) -> RunnerContext:
        return RunnerContext(
            thread_id="thread-1",
            run_id="run-1",
            request_id="request-1",
            messages=[],
            model_id=None,
            mcp_servers=[],
            skills=[],
            reasoning_effort=None,
            attachments=[],
            options=options or {},
            settings=settings,
        )

    def test_network_is_allowed_by_default(self) -> None:
        self.assertTrue(network_access_enabled(self._context()))
        self.assertEqual(
            claude_sandbox_settings(True)["sandbox"]["network"]["allowedDomains"],
            ["*"],
        )

    def test_boolean_run_override_can_deny_network(self) -> None:
        ctx = self._context(options={"networkAccess": False})
        self.assertFalse(network_access_enabled(ctx))
        settings = claude_sandbox_settings(False)
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertEqual(settings["sandbox"]["network"]["allowedDomains"], [])

    def test_claude_full_access_disables_run_sandbox(self) -> None:
        settings = claude_sandbox_settings(True, full_access=True)
        self.assertFalse(settings["sandbox"]["enabled"])

    def test_claude_routine_tools_are_preapproved_and_can_be_overridden(self) -> None:
        enabled = self._context()
        allowed = _claude_allowed_tools(enabled)
        for tool in ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch"):
            self.assertIn(tool, allowed)
        restricted = self._context(options={"claudeAutoApproveTools": ["Read"]})
        self.assertEqual(
            _claude_allowed_tools(restricted),
            ["Read", "mcp__k_agent_human_approval__request_approval"],
        )


class ClaudeInteractiveToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_access_keeps_prompt_bridge_for_ask_user_question(self) -> None:
        ctx = RunnerContext(
            thread_id="thread-full",
            run_id="run-full",
            request_id="request-full",
            messages=[],
            model_id=None,
            mcp_servers=[],
            skills=[],
            reasoning_effort=None,
            attachments=[],
            options={"permissionMode": "full_access"},
            approval_broker=ApprovalBroker(),
        )
        with TemporaryDirectory() as tmp, patch(
            "backend.runners.claude_code.resolve_cli",
            return_value=type("Resolved", (), {"path": "/usr/local/bin/claude"})(),
        ):
            ctx = replace(ctx, workspace_dir=Path(tmp))
            async with ClaudeCodeRunner().create_runtime(ctx) as runtime:
                argv = runtime["argv"]
                self.assertIn("bypassPermissions", argv)
                self.assertIn("--permission-prompt-tool", argv)
                self.assertIn(
                    "mcp__k_agent_human_approval__request_approval", argv
                )


class DetectAgentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_k_agent_always_available_and_missing_cli_marked(self) -> None:
        with patch("backend.runners.resolve_cli.shutil.which", return_value=None), patch(
            "backend.runners.resolve_cli.Path.is_file", return_value=False
        ):
            agents = await detect_agents()
        by_kind = {agent.kind: agent for agent in agents}
        self.assertTrue(by_kind["k_agent"].available)
        self.assertFalse(by_kind["codex"].available)
        self.assertFalse(by_kind["claude_code"].available)

    async def test_resolves_known_chatgpt_codex_path(self) -> None:
        from backend.runners.resolve_cli import resolve_cli

        chatgpt = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if not chatgpt.is_file():
            self.skipTest("ChatGPT.app codex not installed on this host")
        resolved = resolve_cli("codex")
        assert resolved is not None
        self.assertTrue(resolved.path.endswith("/codex"))

    async def test_resolves_claude_internal_on_path(self) -> None:
        from backend.runners.resolve_cli import resolve_cli

        with patch(
            "backend.runners.resolve_cli.shutil.which",
            side_effect=lambda name: "/opt/homebrew/bin/claude-internal"
            if name == "claude-internal"
            else None,
        ), patch("backend.runners.resolve_cli.Path.is_file", return_value=False):
            resolved = resolve_cli("claude_code")
        assert resolved is not None
        self.assertEqual(resolved.path, "/opt/homebrew/bin/claude-internal")
        self.assertEqual(resolved.source, "which")


class CliMapperTests(unittest.TestCase):
    def test_build_prompt_includes_roles(self) -> None:
        messages = [
            ChatMessage(
                id="1",
                role="user",
                content="hello",
                createdAt=datetime.now(timezone.utc),
            ),
            ChatMessage(
                id="2",
                role="assistant",
                content="hi",
                createdAt=datetime.now(timezone.utc),
            ),
        ]
        prompt = build_prompt_from_messages(messages)
        self.assertIn("user: hello", prompt)
        self.assertIn("assistant: hi", prompt)

    def test_extract_latest_user_prompt(self) -> None:
        messages = [
            ChatMessage(id="1", role="user", content="first", createdAt=datetime.now(timezone.utc)),
            ChatMessage(id="2", role="assistant", content="ok", createdAt=datetime.now(timezone.utc)),
            ChatMessage(id="3", role="user", content="second question", createdAt=datetime.now(timezone.utc)),
        ]
        self.assertEqual(extract_latest_user_prompt(messages), "second question")

    def test_extract_latest_user_prompt_no_user(self) -> None:
        messages = [
            ChatMessage(id="1", role="assistant", content="hi", createdAt=datetime.now(timezone.utc)),
        ]
        self.assertEqual(extract_latest_user_prompt(messages), "Continue.")

    def test_codex_agent_message_maps_to_text(self) -> None:
        state = CodexStreamState()
        events = map_codex_event(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            },
            state,
        )
        types = [event["type"] for event in events]
        self.assertIn("message_start", types)
        self.assertIn("delta", types)
        self.assertIn("message_end", types)

    def test_claude_text_delta_maps(self) -> None:
        state = ClaudeCodeStreamState()
        events = map_claude_event(
            {
                "type": "stream_event",
                "event": {"delta": {"type": "text_delta", "text": "Hello"}},
            },
            state,
        )
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[1]["payload"]["content"], "Hello")

    def test_claude_completed_snapshot_does_not_repeat_streamed_text(self) -> None:
        state = ClaudeCodeStreamState()
        first = map_claude_event(
            {
                "type": "stream_event",
                "event": {"delta": {"type": "text_delta", "text": "Hello"}},
            },
            state,
        )
        completed = map_claude_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
            },
            state,
        )
        self.assertEqual(
            [event["payload"]["content"] for event in first + completed if event["type"] == "delta"],
            ["Hello"],
        )

    def test_claude_completed_snapshot_only_appends_missing_suffix(self) -> None:
        state = ClaudeCodeStreamState()
        map_claude_event(
            {
                "type": "stream_event",
                "event": {"delta": {"type": "text_delta", "text": "Hello"}},
            },
            state,
        )
        completed = map_claude_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello world"}]},
            },
            state,
        )
        self.assertEqual(
            [event["payload"]["content"] for event in completed if event["type"] == "delta"],
            [" world"],
        )

    def test_claude_init_captures_session_id(self) -> None:
        state = ClaudeCodeStreamState()
        map_claude_event(
            {"type": "system", "subtype": "init", "session_id": "sess-1"},
            state,
        )
        self.assertEqual(state.provider_session_id, "sess-1")

class CodexRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_uses_bidirectional_app_server(self) -> None:
        ctx = RunnerContext(
            thread_id="thread-1",
            run_id="run-1",
            request_id="request-1",
            messages=[
                ChatMessage(
                    id="message-1",
                    role="user",
                    content="hello",
                    createdAt=datetime.now(timezone.utc),
                )
            ],
            model_id=None,
            mcp_servers=[],
            skills=[],
            reasoning_effort=None,
            attachments=[],
            approval_broker=ApprovalBroker(),
        )
        captured: dict[str, object] = {}

        async def fake_run_app_server(**kwargs):
            captured.update(kwargs)
            if False:
                yield {}

        with TemporaryDirectory() as tmp, TemporaryDirectory() as home, patch.dict(
            "os.environ", {"K_AGENT_HOME": home}, clear=False
        ), patch(
            "backend.runners.codex.resolve_cli",
            return_value=type("Resolved", (), {"path": "/usr/local/bin/codex"})(),
        ), patch("backend.runners.codex.run_codex_app_server", fake_run_app_server):
            from backend.home import reset_home_cache

            reset_home_cache()
            try:
                # The workspace is a request input; Codex must not derive it
                # from thread_id or inspect the Access Layer session bundle.
                ctx = replace(ctx, workspace_dir=Path(tmp))
                events = [event async for event in CodexRunner().run_stream(ctx)]
            finally:
                reset_home_cache()

        self.assertEqual(events, [])
        self.assertEqual(captured["command"], "/usr/local/bin/codex")
        self.assertEqual(captured["public_thread_id"], "thread-1")
        self.assertIs(captured["approval_broker"], ctx.approval_broker)
        self.assertTrue(captured["network_access"])
        env = captured["env"]
        assert isinstance(env, dict)
        self.assertIn("K_AGENT_SHARED_RUNTIME", env)
        self.assertIn("npm_config_prefix", env)
        self.assertTrue(str(env["PATH"]).startswith(str(Path(env["K_AGENT_SHARED_RUNTIME"]) / "node" / "bin")))


class McpConfigAndSkillTests(unittest.TestCase):
    def test_write_mcp_config_stdio(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            servers = [
                {"id": "fs", "type": "stdio", "command": "npx", "args": ["-y", "@mcp/fs"]},
            ]
            path = write_claude_mcp_config(workspace, servers)
            self.assertIsNotNone(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("fs", payload["mcpServers"])
            self.assertEqual(payload["mcpServers"]["fs"]["command"], "npx")
            self.assertEqual(payload["mcpServers"]["fs"]["args"], ["-y", "@mcp/fs"])

    def test_write_mcp_config_http(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            servers = [
                {"id": "remote", "type": "http", "url": "https://example.test/mcp"},
            ]
            path = write_claude_mcp_config(workspace, servers)
            self.assertIsNotNone(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["mcpServers"]["remote"]["url"], "https://example.test/mcp")
            self.assertEqual(payload["mcpServers"]["remote"]["type"], "http")

    def test_write_mcp_config_http_uses_environment_header_references(self) -> None:
        with TemporaryDirectory() as tmp:
            path = write_claude_mcp_config(Path(tmp), [{
                "id": "remote",
                "type": "http",
                "url": "https://example.test/mcp",
                "bearerTokenEnv": "REMOTE_TOKEN",
                "envHeaders": {"X-Tenant": "TENANT_ID"},
            }])
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            remote = payload["mcpServers"]["remote"]
            self.assertTrue(remote["command"].endswith("python") or "python" in remote["command"])
            self.assertTrue(remote["args"][0].endswith("claude_mcp_stdio_proxy.py"))
            self.assertEqual(
                remote["env"],
                {"REMOTE_TOKEN": "${REMOTE_TOKEN}", "TENANT_ID": "${TENANT_ID}"},
            )
            # Credential values never appear in argv or the generated config.
            serialized = json.dumps(payload)
            self.assertNotIn("Bearer test-secret", serialized)

    def test_static_http_headers_stay_out_of_generated_mcp_config(self) -> None:
        server = {
            "id": "remote",
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"X-Secret": "private-value"},
            "bearerTokenEnv": "REMOTE_TOKEN",
        }
        with TemporaryDirectory() as tmp:
            path = write_claude_mcp_config(Path(tmp), [server])
            assert path is not None
            serialized = path.read_text(encoding="utf-8")
            self.assertNotIn("private-value", serialized)
            env: dict[str, str] = {}
            inject_claude_mcp_secrets(env, [server])
            self.assertIn("private-value", env.values())

    def test_write_mcp_config_empty_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(write_claude_mcp_config(Path(tmp), []))

    def test_write_mcp_config_skips_invalid_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            servers = [
                {"id": "", "command": "bad"},
                {"id": "nocommand", "type": "stdio"},
                {"id": "ok", "command": "echo"},
            ]
            path = write_claude_mcp_config(workspace, servers)
            self.assertIsNotNone(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(payload["mcpServers"].keys()), ["ok"])

    def test_claude_skill_preamble_includes_package_paths(self) -> None:
        skills = [
            {
                "name": "Search",
                "instructions": "Run scripts/search.py.",
                "filePath": "/skills/search/SKILL.md",
                "baseDir": "/skills/search",
            },
        ]
        result = build_claude_skill_preamble(skills)
        self.assertIn("[Claude Code Skill: Search]", result)
        self.assertIn("SKILL.md absolute path: /skills/search/SKILL.md", result)
        self.assertIn("Skill package root: /skills/search", result)
        self.assertIn("against the Skill package root above", result)
        self.assertIn("Run scripts/search.py.", result)

    def test_codex_skill_preamble_includes_package_paths(self) -> None:
        skills = [
            {
                "name": "Review",
                "instructions": "Read references/checklist.md.",
                "filePath": "/skills/review/SKILL.md",
                "baseDir": "/skills/review",
            },
        ]
        result = build_codex_skill_preamble(skills)
        self.assertIn("[Codex Skill: Review]", result)
        self.assertIn("SKILL.md absolute path: /skills/review/SKILL.md", result)
        self.assertIn("Skill package root: /skills/review", result)
        self.assertIn("against the Skill package root above", result)
        self.assertIn("Read references/checklist.md.", result)

    def test_provider_skill_preambles_skip_empty_instructions(self) -> None:
        skills = [{"name": "empty", "instructions": ""}]
        self.assertEqual(build_claude_skill_preamble([]), "")
        self.assertEqual(build_codex_skill_preamble(skills), "")

    def test_write_codex_mcp_config_stdio(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            servers = [
                {"id": "fs", "command": "npx", "args": ["-y", "@mcp/fs"], "env": {"KEY": "val"}},
            ]
            path = write_codex_mcp_config(project, servers)
            self.assertIsNotNone(path)
            self.assertEqual(path, project / ".codex" / "config.toml")
            text = path.read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.fs]", text)
            self.assertIn('command = "npx"', text)
            self.assertIn('"@mcp/fs"', text)
            self.assertIn('[mcp_servers.fs.env]', text)
            self.assertIn('KEY = "val"', text)
            self.assertIn("k_agent managed", text)

    def test_write_codex_mcp_config_http(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            servers = [{"id": "remote", "type": "http", "url": "https://example.test/mcp"}]
            path = write_codex_mcp_config(project, servers)
            text = path.read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.remote]", text)
            self.assertIn('url = "https://example.test/mcp"', text)

    def test_write_codex_mcp_config_preserves_user_content(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            codex_dir = project / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text('model = "o3"\n', encoding="utf-8")
            write_codex_mcp_config(project, [{"id": "s", "command": "echo"}])
            text = (codex_dir / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "o3"', text)
            self.assertIn("[mcp_servers.s]", text)

    def test_write_codex_mcp_config_replaces_on_rerun(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_codex_mcp_config(project, [{"id": "old", "command": "old_cmd"}])
            write_codex_mcp_config(project, [{"id": "new", "command": "new_cmd"}])
            text = (project / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("old_cmd", text)
            self.assertIn("[mcp_servers.new]", text)
            self.assertEqual(text.count("k_agent managed MCP servers >>>"), 1)

    def test_write_codex_mcp_config_empty_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(write_codex_mcp_config(Path(tmp), []))


class SessionLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_session_json_and_workspace(self) -> None:
        from access_layer.storage import FileStorage
        from access_layer.sessions.store import SessionStore
        from access_layer.settings import get_or_init_settings
        import access_layer.settings as settings_mod
        import os

        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"K_AGENT_HOME": tmp}, clear=False):
                from access_layer.home import reset_home_cache, ensure_home_layout

                reset_home_cache()
                ensure_home_layout(migrate=False)
                settings_mod._config = None
                settings = await get_or_init_settings()
                storage = FileStorage(settings.storage_base_dir)
                store = SessionStore(storage)
                session = await store.create_session(session_id="abc-123", title="t")
                path = Path(settings.storage_base_dir) / "sessions" / "abc-123" / "abc-123.json"
                workspace = Path(settings.storage_base_dir) / "sessions" / "abc-123" / "workspace"
                self.assertTrue(path.is_file())
                self.assertTrue(workspace.is_dir())
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["id"], session.id)
                self.assertIn("cliSessions", payload)

    async def test_migrates_flat_session_file(self) -> None:
        from access_layer.storage import FileStorage
        from access_layer.sessions.store import SessionStore
        from access_layer.settings import get_or_init_settings
        import access_layer.settings as settings_mod
        import os

        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"K_AGENT_HOME": tmp}, clear=False):
                from access_layer.home import reset_home_cache, ensure_home_layout

                reset_home_cache()
                ensure_home_layout(migrate=False)
                settings_mod._config = None
                settings = await get_or_init_settings()
                root = Path(settings.storage_base_dir) / "sessions"
                root.mkdir(parents=True, exist_ok=True)
                flat = root / "legacy.json"
                flat.write_text(
                    json.dumps(
                        {
                            "id": "legacy",
                            "title": "old",
                            "messages": [],
                            "trace": [],
                            "tasks": [],
                            "thinking": [],
                            "events": [],
                            "updatedAt": "2026-01-01T00:00:00+00:00",
                        }
                    ),
                    encoding="utf-8",
                )
                store = SessionStore(FileStorage(settings.storage_base_dir))
                session = await store.get("legacy")
                assert session is not None
                self.assertEqual(session.title, "old")
                self.assertFalse(flat.exists())
                self.assertTrue((root / "legacy" / "legacy.json").is_file())
                self.assertTrue((root / "legacy" / "workspace").is_dir())


if __name__ == "__main__":
    unittest.main()
