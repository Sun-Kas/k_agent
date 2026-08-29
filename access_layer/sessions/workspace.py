"""会话工作区只读预览：列出/读取 `$K_AGENT_HOME/state/sessions/{id}/workspace`。

在请求链路中的角色：公开 API `/api/sessions/{id}/workspace*` 的文件系统边界；
CLI runner 的 cwd 即该目录，工作台可预览交付物。

服务边界 / 安全：
- 只读；禁止路径穿越与非法 session_id
- 过滤注入的工具配置（如 `.codex/`、`.mcp.json`），不把内部配置暴露给前端
- 实际忽略规则委托 `workspace_fs` 公共实现，本模块只绑定会话根路径
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
from access_layer.home import public_home_relative_path, session_workspace_dir

# Re-export for existing tests / callers.
def is_ignored_workspace_name(name: str) -> bool:
    """判断工作区条目名是否应对外隐藏（工具注入配置等）。"""
    return is_ignored_name(
        name,
        ignored_names=COMMON_IGNORED_NAMES,
        ignored_prefixes=COMMON_IGNORED_PREFIXES,
    )


def resolve_session_workspace(session_id: str) -> Path:
    """解析会话工作区绝对路径；拒绝含路径分隔符或 `.`/`..` 的伪 id。"""
    if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
        raise ValueError("Invalid session id")
    return session_workspace_dir(session_id).resolve()


def list_session_workspace(session_id: str) -> WorkspaceListing:
    """列出可预览文件（过滤忽略项），root 对外显示为相对 $K_AGENT_HOME 的路径。"""
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
    """读取单个工作区文件预览；路径逃逸时失败关闭（fail closed）。"""
    return read_workspace_file(
        resolve_session_workspace(session_id),
        relative_path,
        ignored_names=COMMON_IGNORED_NAMES,
        ignored_prefixes=COMMON_IGNORED_PREFIXES,
    )
