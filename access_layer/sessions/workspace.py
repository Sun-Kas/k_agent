"""Read user-visible files from a conversation's session workspace.

CLI runners keep cwd at `$K_AGENT_HOME/state/sessions/{id}/workspace`. The
chat workbench may preview those deliverables, but must not surface injected
tool config such as `.codex/` or `.mcp.json`.
"""

from __future__ import annotations

from pathlib import Path

from access_layer.workspace_fs import (
    COMMON_IGNORED_NAMES,
    COMMON_IGNORED_PREFIXES,
    WorkspaceFileContent,
    WorkspaceListing,
    is_ignored_name,
    list_workspace_files,
    read_workspace_file,
)
from backend.home import public_home_relative_path, session_workspace_dir

# Re-export for existing tests / callers.
def is_ignored_workspace_name(name: str) -> bool:
    return is_ignored_name(
        name,
        ignored_names=COMMON_IGNORED_NAMES,
        ignored_prefixes=COMMON_IGNORED_PREFIXES,
    )


def resolve_session_workspace(session_id: str) -> Path:
    """Return the absolute workspace directory for a session id."""

    if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
        raise ValueError("Invalid session id")
    return session_workspace_dir(session_id).resolve()


def list_session_workspace(session_id: str) -> WorkspaceListing:
    """List previewable files under the session workspace, newest first."""

    listing = list_workspace_files(
        resolve_session_workspace(session_id),
        ignored_names=COMMON_IGNORED_NAMES,
        ignored_prefixes=COMMON_IGNORED_PREFIXES,
    )
    return WorkspaceListing(
        root=public_home_relative_path(listing.root) or listing.root,
        files=listing.files,
    )


def read_session_workspace_file(session_id: str, relative_path: str) -> WorkspaceFileContent:
    """Read a text preview of one workspace file, failing closed on escapes."""

    return read_workspace_file(
        resolve_session_workspace(session_id),
        relative_path,
        ignored_names=COMMON_IGNORED_NAMES,
        ignored_prefixes=COMMON_IGNORED_PREFIXES,
    )
