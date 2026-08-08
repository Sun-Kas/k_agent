"""Shared filesystem preview helpers for session and Team workspaces."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

# Runtime-injected agent config and VCS/env noise stay out of the preview pane.
COMMON_IGNORED_NAMES = {
    ".codex",
    ".claude",
    ".mcp",
    ".mcp.json",
    ".cursor",
    ".runtime",
    ".git",
    ".gitignore",
    ".gitattributes",
    ".env",
    ".env.local",
    ".DS_Store",
    "__pycache__",
    "node_modules",
}

# Team publication plumbing / ownership markers are not user deliverables.
TEAM_IGNORED_NAMES = {
    *COMMON_IGNORED_NAMES,
    ".k_agent-staging",
    ".k_agent-team.json",
    "artifacts",
}

COMMON_IGNORED_PREFIXES = (".codex", ".mcp", ".claude", ".cursor")
TEAM_IGNORED_PREFIXES = (*COMMON_IGNORED_PREFIXES, ".k_agent")

_MAX_PREVIEW_BYTES = 200_000
_MAX_LISTED_FILES = 400


@dataclass(frozen=True)
class WorkspaceFileInfo:
    path: str
    name: str
    size: int
    modified_at: float


@dataclass(frozen=True)
class WorkspaceListing:
    root: str
    files: list[WorkspaceFileInfo]


@dataclass(frozen=True)
class WorkspaceFileContent:
    path: str
    name: str
    content: str
    truncated: bool
    binary: bool
    size: int


def is_ignored_name(
    name: str,
    *,
    ignored_names: set[str],
    ignored_prefixes: tuple[str, ...] = COMMON_IGNORED_PREFIXES,
) -> bool:
    """Return True when a basename should stay hidden from the workbench."""

    if not name or name in {".", ".."}:
        return True
    if name in ignored_names:
        return True
    lowered = name.lower()
    return any(
        lowered == prefix
        or lowered.startswith(f"{prefix}.")
        or lowered.startswith(f"{prefix}-")
        for prefix in ignored_prefixes
    )


def list_workspace_files(
    root: Path,
    *,
    ignored_names: set[str],
    ignored_prefixes: tuple[str, ...] = COMMON_IGNORED_PREFIXES,
) -> WorkspaceListing:
    """List previewable files under root, newest first."""

    resolved = root.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    files: list[WorkspaceFileInfo] = []
    for path in resolved.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(resolved)
        except ValueError:
            continue
        if any(
            is_ignored_name(
                part, ignored_names=ignored_names, ignored_prefixes=ignored_prefixes
            )
            for part in relative.parts
        ):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            WorkspaceFileInfo(
                path=relative.as_posix(),
                name=path.name,
                size=stat.st_size,
                modified_at=stat.st_mtime,
            )
        )
        if len(files) >= _MAX_LISTED_FILES:
            break
    files.sort(key=lambda item: (-item.modified_at, item.path.lower()))
    return WorkspaceListing(root=str(resolved), files=files)


def read_workspace_file(
    root: Path,
    relative_path: str,
    *,
    ignored_names: set[str],
    ignored_prefixes: tuple[str, ...] = COMMON_IGNORED_PREFIXES,
) -> WorkspaceFileContent:
    """Read a text preview of one workspace file, failing closed on escapes."""

    resolved_root = root.expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    candidate = resolve_inside_workspace(resolved_root, relative_path)
    relative = candidate.relative_to(resolved_root)
    if any(
        is_ignored_name(
            part, ignored_names=ignored_names, ignored_prefixes=ignored_prefixes
        )
        for part in relative.parts
    ):
        raise FileNotFoundError("Workspace file not found")
    if not candidate.is_file():
        raise FileNotFoundError("Workspace file not found")

    size = candidate.stat().st_size
    raw = candidate.read_bytes()[: _MAX_PREVIEW_BYTES + 1]
    truncated = len(raw) > _MAX_PREVIEW_BYTES
    raw = raw[:_MAX_PREVIEW_BYTES]
    if looks_binary(candidate, raw):
        return WorkspaceFileContent(
            path=relative.as_posix(),
            name=candidate.name,
            content="",
            truncated=truncated,
            binary=True,
            size=size,
        )
    text = raw.decode("utf-8", errors="replace")
    return WorkspaceFileContent(
        path=relative.as_posix(),
        name=candidate.name,
        content=text,
        truncated=truncated,
        binary=False,
        size=size,
    )


def resolve_inside_workspace(root: Path, relative_path: str) -> Path:
    cleaned = (relative_path or "").replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        raise ValueError("Invalid workspace path")
    resolved = (root / cleaned).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Workspace path escapes workspace root") from exc
    return resolved


def looks_binary(path: Path, sample: bytes) -> bool:
    mime, _ = mimetypes.guess_type(path.name)
    if mime and not mime.startswith("text/") and mime not in {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/typescript",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/sql",
    }:
        if path.suffix.lower() not in {
            ".md", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx", ".json",
            ".yml", ".yaml", ".toml", ".csv", ".html", ".css", ".sh", ".rs",
            ".go", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp",
            ".sql", ".xml", ".ini", ".cfg", ".log", ".env.example",
        }:
            return True
    if b"\x00" in sample:
        return True
    return False
