"""Resolve local Codex / Claude Code CLI binaries beyond bare PATH names.

Desktop installs often ship the binary outside PATH (ChatGPT.app Resources),
and some forks expose an alternate command name such as `claude-internal`.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ResolvedCli:
    kind: str
    path: str
    source: str  # path | env | which | known_path


_HOME = Path.home()

# kind -> (env overrides, PATH candidate names, absolute fallbacks)
_RESOLVERS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[Path, ...]]] = {
    "codex": (
        ("K_AGENT_CODEX_PATH", "CODEX_CLI_PATH"),
        ("codex",),
        (
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            _HOME / "Applications/ChatGPT.app/Contents/Resources/codex",
            Path("/Applications/Codex.app/Contents/Resources/codex"),
            _HOME / ".local/bin/codex",
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
        ),
    ),
    "claude_code": (
        ("K_AGENT_CLAUDE_PATH", "CLAUDE_CLI_PATH"),
        # Official CLI is `claude`; Tencent internal fork ships `claude-internal`.
        ("claude", "claude-internal", "tclaude"),
        (
            _HOME / ".local/bin/claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/opt/homebrew/bin/claude-internal"),
            Path("/usr/local/bin/claude"),
            Path("/usr/local/bin/claude-internal"),
            Path(
                "/opt/homebrew/lib/node_modules/@tencent/claude-code-internal"
                "/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
            ),
            Path(
                "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
            ),
        ),
    ),
}


def resolve_cli(kind: str) -> ResolvedCli | None:
    """Return the first usable CLI path for an agent kind, if any."""

    spec = _RESOLVERS.get(kind)
    if spec is None:
        return None
    env_keys, names, known_paths = spec

    for key in env_keys:
        configured = (os.getenv(key) or "").strip()
        if not configured:
            continue
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return ResolvedCli(kind=kind, path=str(path.resolve()), source="env")

    for name in names:
        found = shutil.which(name)
        if found:
            return ResolvedCli(kind=kind, path=found, source="which")

    for path in known_paths:
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return ResolvedCli(kind=kind, path=str(path.resolve()), source="known_path")
        except OSError:
            continue
    return None


def cli_display_name(kind: str, resolved: ResolvedCli | None) -> str | None:
    if resolved is None:
        return None
    return Path(resolved.path).name
