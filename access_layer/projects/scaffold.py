"""新建 Project 时的最小目录骨架：git init + .gitignore + README.md。

只在 ProjectStore.create() 真正新建记录（而不是复用已登记的已有 root_path）
时调用一次。每一步都是"不存在才创建"的幂等操作——Project 的 root_path 允许
指向任意已存在的目录，包括用户直接选中一个早就在用的真实 git 仓库；如果不
做判断就无脑 `git init`/覆盖 `.gitignore`/`README.md`，会污染或破坏用户已有
的项目。所以：已经有 `.git` 就跳过 init，已经有 `.gitignore`/`README.md` 就
不覆盖。只有真正"从零开始的空文件夹"才会触发完整的三步。

刻意不做的事：不打 initial commit（首次提交的时机交给用户/Agent 自己决定），
不生成 .gitattributes、CI 配置、LICENSE、package.json/pyproject.toml 这类
技术栈相关文件——Project 的设计初衷是"通用的一个文件夹"，不该替用户假设
技术栈。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from access_layer.workspace_fs import COMMON_IGNORED_NAMES

logger = logging.getLogger("k_agent.access_layer.projects")

# COMMON_IGNORED_NAMES 里既有目录名（node_modules）也有具体文件名
# （.env、.DS_Store），直接按原样当成 .gitignore 的行即可；目录项补一条
# 尾部斜杠是 git 习惯写法，但不加也完全等价，这里不额外处理以保持列表
# 和"预览时隐藏哪些文件"的那份名单逐字一致、单一数据源。
_GITIGNORE_HEADER = (
    "# 由 K Agent 在新建这个 Project 时自动生成，\n"
    "# 挡掉 Agent 运行过程中必然产生的运行时噪音。\n"
)


def scaffold_new_project(root: Path, display_name: str) -> None:
    """让 root 这个目录具备最基础的仓库骨架；任何一步失败都只记日志，不阻断创建流程。

    这是同步/阻塞的（subprocess + 文件 IO），调用方（ProjectStore.create）
    负责用 asyncio.to_thread 包一层，避免占住事件循环。
    """

    _ensure_git_initialized(root)
    _ensure_gitignore(root)
    _ensure_readme(root, display_name)


def _ensure_git_initialized(root: Path) -> None:
    if (root / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "init"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # 本机没装 git、权限问题等：Project 本身不依赖 git 才能工作
        # （root_path 就是一个普通目录），所以这里失败不应该让创建 Project 失败。
        logger.warning("git init failed for new Project at %s: %s", root, exc)


def _ensure_gitignore(root: Path) -> None:
    target = root / ".gitignore"
    if target.exists():
        return
    body = _GITIGNORE_HEADER + "\n".join(sorted(COMMON_IGNORED_NAMES)) + "\n"
    try:
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write .gitignore for new Project at %s: %s", root, exc)


def _ensure_readme(root: Path, display_name: str) -> None:
    target = root / "README.md"
    if target.exists():
        return
    try:
        target.write_text(f"# {display_name}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write README.md for new Project at %s: %s", root, exc)
