"""Project 工作区只读预览：直接读 Project 的 root_path（可在磁盘任意位置）。

和 access_layer/sessions/workspace.py 的差别：Session workspace 永远落在
$K_AGENT_HOME 下；Project 的 root_path 是用户指定目录，可能完全在外面，所以
这里用 resolve_managed_path 而不是任何 session_* 路径拼接。
"""

from __future__ import annotations

from pathlib import Path

from access_layer.home import public_home_relative_path, resolve_managed_path
from access_layer.workspace_fs import (
    COMMON_IGNORED_NAMES,
    COMMON_IGNORED_PREFIXES,
    WorkspaceFileContent,
    WorkspaceListing,
    list_workspace_files,
    read_workspace_file,
)


def list_project_workspace(root_path: str | Path) -> WorkspaceListing:
    """列出 Project 根目录下可预览的文件（过滤工具注入配置）。"""

    listing = list_workspace_files(
        resolve_managed_path(root_path),
        ignored_names=COMMON_IGNORED_NAMES,
        ignored_prefixes=COMMON_IGNORED_PREFIXES,
    )
    return WorkspaceListing(
        root=public_home_relative_path(listing.root) or listing.root,
        files=listing.files,
    )


def read_project_workspace_file(root_path: str | Path, relative_path: str) -> WorkspaceFileContent:
    """读取 Project 根目录下单个文件预览；路径逃逸时失败关闭。"""

    return read_workspace_file(
        resolve_managed_path(root_path),
        relative_path,
        ignored_names=COMMON_IGNORED_NAMES,
        ignored_prefixes=COMMON_IGNORED_PREFIXES,
    )
