"""Request-scoped workspace boundary for local file and shell tools."""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path


_workspace_root: ContextVar[Path | None] = ContextVar(
    "k_agent_tool_workspace_root", default=None
)
_network_access: ContextVar[bool | None] = ContextVar(
    "k_agent_tool_network_access", default=None
)


def set_tool_workspace(path: Path | None) -> Token[Path | None]:
    """Bind a workspace to the current Agent run without mutating global settings."""

    return _workspace_root.set(path.resolve() if path is not None else None)


def reset_tool_workspace(token: Token[Path | None]) -> None:
    """Restore the prior workspace when a streamed run closes."""

    _workspace_root.reset(token)


def current_tool_workspace() -> Path | None:
    """Return the workspace inherited by tools in the current async context."""

    return _workspace_root.get()


def set_tool_network_access(enabled: bool) -> Token[bool | None]:
    """Bind the run's outbound policy for Bash without mutating Settings."""

    return _network_access.set(enabled)


def reset_tool_network_access(token: Token[bool | None]) -> None:
    """Restore the previous async-context network policy."""

    _network_access.reset(token)


def current_tool_network_access() -> bool | None:
    """Return the run override inherited by request-scoped local tools."""

    return _network_access.get()
