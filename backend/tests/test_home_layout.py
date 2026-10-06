from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.home import (
    ensure_home_layout,
    ensure_shared_runtime,
    mcp_catalog_path,
    memory_dir,
    models_config_path,
    reset_home_cache,
    session_workspace_dir,
    sessions_dir,
    shared_runtime_prefix,
    shared_runtime_tool_env,
    skills_catalog_path,
    skills_dir,
)


class HomeLayoutTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_home_cache()

    def test_relative_home_resolves_against_project(self) -> None:
        with TemporaryDirectory() as tmp:
            reset_home_cache()
            with patch.dict(os.environ, {"K_AGENT_HOME": tmp}, clear=False):
                reset_home_cache()
                ensure_home_layout()
                self.assertTrue(sessions_dir().is_dir())
                self.assertTrue(memory_dir().is_dir())
                self.assertTrue(skills_dir().is_dir())
                self.assertEqual(session_workspace_dir("demo"), sessions_dir() / "demo" / "workspace")
                self.assertEqual(mcp_catalog_path().name, "mcp.json")
                self.assertEqual(skills_catalog_path().name, "skills.json")
                self.assertEqual(models_config_path().name, "models.json")

    def test_tool_env_keeps_workspace_runtime_symlink(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = Path(tmp) / "workspace"
            reset_home_cache()
            with patch.dict(os.environ, {"K_AGENT_HOME": str(home)}, clear=False):
                reset_home_cache()
                prefix = shared_runtime_prefix(workspace)
                env = shared_runtime_tool_env(prefix)
                self.assertEqual(Path(env["K_AGENT_SHARED_RUNTIME"]), workspace / ".runtime")
                self.assertTrue((workspace / ".runtime").is_symlink())
                self.assertEqual((workspace / ".runtime").resolve(), ensure_shared_runtime())
                self.assertNotEqual(Path(env["K_AGENT_SHARED_RUNTIME"]), ensure_shared_runtime())


if __name__ == "__main__":
    unittest.main()
