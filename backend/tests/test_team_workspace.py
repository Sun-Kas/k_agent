from __future__ import annotations

from pathlib import Path

import pytest

from access_layer.teams.workspace import list_team_workspace, read_team_workspace_file
from access_layer.workspace_fs import TEAM_IGNORED_NAMES, is_ignored_name


def test_team_ignores_system_names() -> None:
    assert is_ignored_name(
        ".k_agent-staging", ignored_names=TEAM_IGNORED_NAMES, ignored_prefixes=(".k_agent",)
    )
    assert is_ignored_name(
        ".k_agent-team.json", ignored_names=TEAM_IGNORED_NAMES, ignored_prefixes=(".k_agent",)
    )
    assert is_ignored_name(
        "artifacts", ignored_names=TEAM_IGNORED_NAMES, ignored_prefixes=(".k_agent",)
    )
    assert not is_ignored_name(
        "index.html", ignored_names=TEAM_IGNORED_NAMES, ignored_prefixes=(".k_agent",)
    )


def test_lists_team_deliverables_and_skips_system_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Site", encoding="utf-8")
    (workspace / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / ".k_agent-team.json").write_text('{"teamId":"t"}', encoding="utf-8")
    staging = workspace / ".k_agent-staging"
    staging.mkdir()
    (staging / "tmp.txt").write_text("x", encoding="utf-8")
    artifacts = workspace / "artifacts" / "task_1" / "artifact_1"
    artifacts.mkdir(parents=True)
    (artifacts / "result.md").write_text("hidden", encoding="utf-8")

    listing = list_team_workspace(workspace)
    paths = {item.path for item in listing.files}
    assert paths == {"README.md", "index.html"}
    # Outside $K_AGENT_HOME there is no relative home form; absolute is retained.
    assert listing.root == str(workspace.resolve())

    payload = read_team_workspace_file(workspace, "README.md")
    assert payload.content.startswith("# Site")
    with pytest.raises(FileNotFoundError):
        read_team_workspace_file(workspace, "artifacts/task_1/artifact_1/result.md")
