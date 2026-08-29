"""Read user-visible files from a Team collaboration workspace."""

from __future__ import annotations

from pathlib import Path

from access_layer.workspace_fs import (
    TEAM_IGNORED_NAMES,
    TEAM_IGNORED_PREFIXES,
    WorkspaceFileContent,
    WorkspaceListing,
    list_workspace_files,
    read_workspace_file,
)
from access_layer.home import public_home_relative_path, resolve_managed_path


def list_team_workspace(workspace_dir: str | Path) -> WorkspaceListing:
    """List deliverable files under a Team workspace, skipping system folders."""

    listing = list_workspace_files(
        resolve_managed_path(workspace_dir),
        ignored_names=TEAM_IGNORED_NAMES,
        ignored_prefixes=TEAM_IGNORED_PREFIXES,
    )
    return WorkspaceListing(
        root=public_home_relative_path(listing.root) or listing.root,
        files=listing.files,
    )


def read_team_workspace_file(
    workspace_dir: str | Path, relative_path: str
) -> WorkspaceFileContent:
    """Read one Team workspace file preview."""

    return read_workspace_file(
        resolve_managed_path(workspace_dir),
        relative_path,
        ignored_names=TEAM_IGNORED_NAMES,
        ignored_prefixes=TEAM_IGNORED_PREFIXES,
    )
