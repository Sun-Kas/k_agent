"""请求级工作区与出网策略：用 ContextVar 绑定，避免改全局 Settings。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path


_workspace_root: ContextVar[Path | None] = ContextVar(
    "k_agent_tool_workspace_root", default=None
)
_network_access: ContextVar[bool | None] = ContextVar(
    "k_agent_tool_network_access", default=None
)
_permission_mode: ContextVar[str] = ContextVar(
    "k_agent_tool_permission_mode", default="default"
)


def set_tool_workspace(path: Path | None) -> Token[Path | None]:
    """把工作区绑到当前异步上下文；run 结束必须 `reset_tool_workspace`。"""

    return _workspace_root.set(path.resolve() if path is not None else None)


def reset_tool_workspace(token: Token[Path | None]) -> None:
    """流式 run 关闭时恢复先前的 workspace ContextVar。"""

    _workspace_root.reset(token)


def current_tool_workspace() -> Path | None:
    """本地工具继承的当前异步上下文工作区。"""

    return _workspace_root.get()


def set_tool_network_access(enabled: bool) -> Token[bool | None]:
    """为本轮 Bash 绑定出网策略覆盖，不改 Settings。"""

    return _network_access.set(enabled)


def reset_tool_network_access(token: Token[bool | None]) -> None:
    """恢复先前的异步上下文出网策略。"""

    _network_access.reset(token)


def current_tool_network_access() -> bool | None:
    """本轮对本地工具生效的出网覆盖；None 表示沿用 Settings 默认。"""

    return _network_access.get()


def set_tool_permission_mode(mode: str) -> Token[str]:
    """Bind the validated run-level permission boundary to local tools."""

    return _permission_mode.set("full_access" if mode == "full_access" else "default")


def reset_tool_permission_mode(token: Token[str]) -> None:
    """Restore the previous permission mode when a streamed run closes."""

    _permission_mode.reset(token)


def current_tool_permission_mode() -> str:
    """Return the current run permission mode without consulting global settings."""

    return _permission_mode.get()
