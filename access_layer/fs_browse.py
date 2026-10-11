"""本机目录浏览：给"新建 Project"弹窗选目录用的最小文件系统接口。

这不是通用文件管理器——只列目录（不列文件），只支持"进入子目录"和
"新建子目录"两个动作，专门给前端在创建 Project 时模拟系统原生的
"选择或新建文件夹"体验。之所以需要这个接口而不是用浏览器原生的
File System Access API：浏览器出于安全考虑不会把选中目录的磁盘绝对路径
交给网页（只给一个沙盒句柄），而 Project 的 root_path 必须是一个真实、
可在会话运行时当 cwd 用的绝对路径。Access Layer 本来就是本机进程、默认
只监听 127.0.0.1，和它已经允许的"Project root_path 可以是磁盘任意位置"
（见 access_layer/projects/store.py）是同一个信任边界，这里只是把"列出
有哪些文件夹可选"这一步也挪到服务端来做。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 浏览时隐藏的噪音目录：点号开头的隐藏目录，以及几个体积大、几乎不可能是
# 用户想选的 Project 根目录的目录名。这只影响"浏览时看不看得到"，不影响
# 创建 Project 的校验（真正的黑名单在 validate_and_prepare_root_path）。
_HIDDEN_NAMES = {"node_modules", "__pycache__", "System Volume Information", "$RECYCLE.BIN"}


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    path: str


@dataclass(frozen=True)
class DirectoryListing:
    path: str
    parent: str | None
    entries: list[DirectoryEntry]


def _is_hidden(name: str) -> bool:
    return name.startswith(".") or name in _HIDDEN_NAMES


def default_browse_root() -> Path:
    """未指定路径时，从用户主目录开始浏览——这是用户最可能找 Project 的地方。"""

    return Path.home()


def list_directories(raw_path: str | None) -> DirectoryListing:
    """列出 raw_path 下的直接子目录（只列文件夹，按名称排序）。"""

    base = Path(raw_path).expanduser().resolve() if raw_path else default_browse_root()
    if not base.exists():
        raise FileNotFoundError(f"目录不存在：{base}")
    if not base.is_dir():
        raise NotADirectoryError(f"不是一个目录：{base}")

    try:
        children = sorted(base.iterdir(), key=lambda item: item.name.lower())
    except PermissionError as exc:
        raise PermissionError(f"没有权限读取：{base}") from exc

    entries: list[DirectoryEntry] = []
    for child in children:
        if _is_hidden(child.name):
            continue
        try:
            if not child.is_dir():
                continue
        except OSError:
            # 断开的 symlink 等：跳过而不是让整个列表失败。
            continue
        entries.append(DirectoryEntry(name=child.name, path=str(child)))

    parent = None if base.parent == base else str(base.parent)
    return DirectoryListing(path=str(base), parent=parent, entries=entries)


def create_directory(parent_path: str, name: str) -> DirectoryEntry:
    """在 parent_path 下新建一个子目录（弹窗里"新建文件夹"按钮用）。"""

    cleaned = (name or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."}:
        raise ValueError("文件夹名称不合法")

    parent = Path(parent_path).expanduser().resolve()
    if not parent.is_dir():
        raise NotADirectoryError(f"不是一个目录：{parent}")

    target = parent / cleaned
    if target.exists():
        raise FileExistsError(f"已存在同名目录：{target}")

    target.mkdir(parents=False, exist_ok=False)
    return DirectoryEntry(name=target.name, path=str(target))
