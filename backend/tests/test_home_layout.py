from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend import home as home_mod
from backend.home import (
    ensure_home_layout,
    mcp_catalog_path,
    mcp_config_path,
    memory_dir,
    migrate_legacy_home,
    models_config_path,
    reset_home_cache,
    sessions_dir,
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
                ensure_home_layout(migrate=False)
                self.assertTrue(sessions_dir().is_dir())
                self.assertTrue(memory_dir().is_dir())
                self.assertTrue(skills_dir().is_dir())
                self.assertEqual(mcp_catalog_path().name, "mcp.json")
                self.assertEqual(skills_catalog_path().name, "skills.json")
                self.assertEqual(models_config_path().name, "models.json")

    def test_migrates_legacy_data_when_destination_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            legacy_data = Path(tmp) / "data"
            legacy_runtime = Path(tmp) / "runtime"
            (legacy_data / "sessions").mkdir(parents=True)
            (legacy_data / "sessions" / "s1.json").write_text("{}", encoding="utf-8")
            (legacy_data / "memory").mkdir(parents=True)
            (legacy_data / "memory" / "MEMORY.md").write_text("# m\n", encoding="utf-8")
            (legacy_data / "skill" / "demo").mkdir(parents=True)
            (legacy_data / "skill" / "demo" / "SKILL.md").write_text("x", encoding="utf-8")
            (legacy_data / "mcp.json").write_text(
                json.dumps({"servers": [{"id": "a", "name": "A", "enabled": True}]}),
                encoding="utf-8",
            )
            (legacy_data / "skill.json").write_text(
                json.dumps({"skills": [{"id": "demo", "name": "Demo", "enabled": True}]}),
                encoding="utf-8",
            )
            legacy_runtime.mkdir(parents=True)
            (legacy_runtime / "mcp.config.json").write_text(
                json.dumps({"servers": []}), encoding="utf-8"
            )
            (legacy_runtime / "models.config.json").write_text(
                json.dumps({"models": []}), encoding="utf-8"
            )

            reset_home_cache()
            with (
                patch.dict(os.environ, {"K_AGENT_HOME": str(home)}, clear=False),
                patch.object(home_mod, "LEGACY_DATA_DIR", legacy_data),
                patch.object(home_mod, "LEGACY_RUNTIME_DIR", legacy_runtime),
            ):
                reset_home_cache()
                ensure_home_layout(migrate=True)
                self.assertTrue((sessions_dir() / "s1.json").exists())
                self.assertTrue((memory_dir() / "MEMORY.md").exists())
                self.assertTrue((skills_dir() / "demo" / "SKILL.md").exists())
                self.assertTrue(mcp_catalog_path().exists())
                self.assertTrue(skills_catalog_path().exists())
                self.assertTrue(mcp_config_path().exists())
                self.assertTrue(models_config_path().exists())
                # Second migrate must not overwrite / duplicate.
                (sessions_dir() / "s1.json").write_text('{"kept":true}', encoding="utf-8")
                reset_home_cache()
                home_mod._migrated = False
                migrate_legacy_home()
                self.assertIn("kept", (sessions_dir() / "s1.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
