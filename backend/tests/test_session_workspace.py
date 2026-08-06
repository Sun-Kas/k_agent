from __future__ import annotations

from pathlib import Path

import pytest

from access_layer.sessions.workspace import (
    is_ignored_workspace_name,
    list_session_workspace,
    read_session_workspace_file,
)


def test_ignores_runtime_config_names() -> None:
    assert is_ignored_workspace_name(".codex")
    assert is_ignored_workspace_name(".mcp.json")
    assert is_ignored_workspace_name(".mcp")
    assert is_ignored_workspace_name(".claude")
    assert not is_ignored_workspace_name("notes.md")
    assert not is_ignored_workspace_name("report.json")


def test_lists_user_files_and_skips_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K_AGENT_HOME", str(tmp_path / "home"))
    from backend.home import reset_home_cache, session_workspace_dir

    reset_home_cache()
    session_id = "sess-demo"
    workspace = session_workspace_dir(session_id)
    workspace.mkdir(parents=True)
    (workspace / "notes.md").write_text("# Hello\n\nBody", encoding="utf-8")
    (workspace / ".mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    codex = workspace / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text("model = \"x\"", encoding="utf-8")
    nested = workspace / "out"
    nested.mkdir()
    (nested / "result.txt").write_text("done", encoding="utf-8")

    listing = list_session_workspace(session_id)
    paths = {item.path for item in listing.files}
    assert paths == {"notes.md", "out/result.txt"}
    assert listing.root == "state/sessions/sess-demo/workspace"


def test_reads_text_and_blocks_config_and_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K_AGENT_HOME", str(tmp_path / "home"))
    from backend.home import reset_home_cache, session_workspace_dir

    reset_home_cache()
    session_id = "sess-read"
    workspace = session_workspace_dir(session_id)
    workspace.mkdir(parents=True)
    (workspace / "notes.md").write_text("# Title\n", encoding="utf-8")
    (workspace / ".mcp.json").write_text("{}", encoding="utf-8")

    payload = read_session_workspace_file(session_id, "notes.md")
    assert payload.content.startswith("# Title")
    assert payload.binary is False

    with pytest.raises(FileNotFoundError):
        read_session_workspace_file(session_id, ".mcp.json")
    with pytest.raises(ValueError):
        read_session_workspace_file(session_id, "../notes.md")
