from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend import home as home_mod
from backend.home import ensure_home_layout, reset_home_cache, teams_dir, to_managed_path
from backend.main import _team_workspace


class TeamWorkspaceValidationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_home_cache()

    def test_relative_managed_workspace_resolves_under_home(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with patch.dict(os.environ, {"K_AGENT_HOME": str(home)}, clear=False):
                reset_home_cache()
                ensure_home_layout(migrate=False)
                control = teams_dir() / "team_demo" / "supervisor" / "job_1"
                control.mkdir(parents=True)
                managed = to_managed_path(control)
                self.assertFalse(Path(managed).is_absolute())
                # Simulates Access Layer cwd = project root while home is elsewhere.
                with patch.object(Path, "cwd", return_value=Path(tmp) / "unrelated-cwd"):
                    resolved = _team_workspace(managed)
                assert resolved is not None
                self.assertEqual(resolved, control.resolve())
                self.assertTrue(resolved.is_relative_to(teams_dir().resolve()))

    def test_path_outside_teams_root_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            outside = Path(tmp) / "outside" / "workspace"
            outside.mkdir(parents=True)
            with patch.dict(os.environ, {"K_AGENT_HOME": str(home)}, clear=False):
                reset_home_cache()
                ensure_home_layout(migrate=False)
                with self.assertRaisesRegex(ValueError, "Team Runtime state root"):
                    _team_workspace(str(outside))


if __name__ == "__main__":
    unittest.main()
