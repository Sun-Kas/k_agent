"""CLI 运行参数不得覆盖 Project 中的用户配置或污染 Git 工作树。"""

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.approvals import ApprovalBroker
from backend.runners.base import RunnerContext
from backend.runners.codex import CodexRunner
from backend.runners.claude_code import ClaudeCodeRunner


def context(workspace: Path) -> RunnerContext:
    return RunnerContext(
        thread_id="session", run_id="run", request_id="request",
        messages=[], model_id=None, reasoning_effort="high", attachments=[],
        workspace_dir=workspace, project_id="project", skills=[],
        mcp_servers=[{"id": "test", "command": "echo", "args": ["hello"]}],
        approval_broker=ApprovalBroker(),
    )


@asynccontextmanager
async def no_bridge(_context):
    yield None


class RunnerConfigIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_passes_runtime_config_without_changing_project_config(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".codex").mkdir()
            config = root / ".codex" / "config.toml"
            config.write_text('model = "user-model"\n')
            with patch("backend.runners.codex.resolve_cli", return_value=SimpleNamespace(path="codex")), patch(
                "backend.runners.codex.build_cli_child_env", return_value={}
            ):
                async with CodexRunner().create_runtime(context(root)) as runtime:
                    self.assertEqual(runtime["cwd"], root)
                    self.assertEqual(runtime["config_overrides"]["mcp_servers"]["test"]["command"], "echo")
            self.assertEqual(config.read_text(), 'model = "user-model"\n')
            self.assertEqual(sorted(path.name for path in root.iterdir()), [".codex"])

    async def test_claude_uses_temporary_config_and_preserves_project_mcp(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / ".mcp.json"
            config.write_text('{"user": "configuration"}\n')
            with patch("backend.runners.claude_code.resolve_cli", return_value=SimpleNamespace(path="claude")), patch(
                "backend.runners.claude_code._approval_bridge", no_bridge
            ), patch("backend.runners.claude_code.build_cli_child_env", return_value={}):
                async with ClaudeCodeRunner().create_runtime(context(root)) as runtime:
                    argv = runtime["argv"]
                    generated = Path(argv[argv.index("--mcp-config") + 1])
                    self.assertNotEqual(generated.parent, root)
                    self.assertTrue(generated.is_file())
                    self.assertEqual(config.read_text(), '{"user": "configuration"}\n')
                self.assertFalse(generated.exists())
