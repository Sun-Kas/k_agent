"""`$K_AGENT_HOME` 持久布局：会话/Team/记忆/配置/共享 runtime 的唯一真相源。

默认 `~/.k_agent`；`K_AGENT_HOME` 相对路径相对仓库根解析。Access Layer 与
Agent Backend 都通过本模块定位路径；相对路径入库时用 `to_managed_path`，
读回时用 `resolve_managed_path`，避免部署 cwd 不一致。

```
$K_AGENT_HOME/
  config/
    mcp.json                 # 托管 MCP 连接
    user-mcp.json            # 可选用户覆盖
    models.json
    permissions.json
    catalog/
      mcp.json / skills.json # 前端选择器摘要
  cache/runtime/             # 全项目共享 Node/npm（会话与 Team 共用）
  state/
    sessions/{id}/           # 会话 JSON + workspace/
    teams/                   # Team 控制面与任务目录
  content/
    memory/ / skills/
```
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[1]

_home_cache: Path | None = None


def reset_home_cache() -> None:
    """测试/重配：丢弃缓存的 home 解析。"""

    global _home_cache
    _home_cache = None


def agent_home() -> Path:
    """解析 `$K_AGENT_HOME`；未设置时默认 `~/.k_agent`（进程内缓存）。"""

    global _home_cache
    if _home_cache is not None:
        return _home_cache
    configured = (os.getenv("K_AGENT_HOME") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = PROJECT_DIR / path
        _home_cache = path.resolve()
    else:
        _home_cache = (Path.home() / ".k_agent").resolve()
    return _home_cache


def config_dir() -> Path:
    return agent_home() / "config"


def catalog_dir() -> Path:
    return config_dir() / "catalog"


def state_dir() -> Path:
    return agent_home() / "state"


def content_dir() -> Path:
    return agent_home() / "content"


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def teams_dir() -> Path:
    """Team Runtime 持久根：元数据、工件与隔离工作区。"""

    return state_dir() / "teams"


def shared_runtime_dir() -> Path:
    """全项目共享的 Node/npm 工具前缀（所有 session/Team 共用一份）。"""

    return agent_home() / "cache" / "runtime"


def ensure_shared_runtime() -> Path:
    """确保 `cache/runtime/{node,npm-cache,projects}` 目录存在。"""

    runtime = shared_runtime_dir().resolve()
    (runtime / "npm-cache").mkdir(parents=True, exist_ok=True)
    (runtime / "node").mkdir(parents=True, exist_ok=True)
    (runtime / "projects").mkdir(parents=True, exist_ok=True)
    return runtime


def _is_dir_link(path: Path) -> bool:
    """True for symlinks or Windows directory junctions."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _remove_path(path: Path) -> None:
    """Remove a file, symlink, junction, or directory tree."""

    if _is_dir_link(path):
        # unlink/rmdir removes the link itself, not the shared runtime target.
        try:
            path.unlink()
        except OSError:
            os.rmdir(path)
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    if path.exists():
        path.unlink()


def _link_directory(link: Path, target: Path) -> None:
    """Create a directory symlink, falling back to a Windows junction.

    Plain users on Windows often lack SeCreateSymbolicLinkPrivilege
    (WinError 1314) unless Developer Mode or elevation is enabled. Directory
    junctions do not require that privilege and still give a stable `.runtime`
    path inside each workspace.
    """

    symlink_winerror: int | None = None
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        symlink_winerror = getattr(exc, "winerror", None)
        if os.name != "nt" or symlink_winerror not in {1314, 1}:
            raise
        logger.info(
            "symlink privilege missing (%s); using directory junction for %s",
            symlink_winerror,
            link,
        )

    # mklink /J creates a junction without admin rights.
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not link.exists():
        detail = (completed.stderr or completed.stdout or "").strip()
        raise OSError(
            symlink_winerror or 1314,
            detail or "failed to create directory junction for shared runtime",
            str(link),
        )


def link_shared_runtime(target_dir: Path, runtime: Path | None = None) -> Path:
    """在工作区挂 `.runtime` → 共享 runtime；过期链接先拆再建。"""

    # 每个 workspace 只挂入口 `.runtime`；真实 Node/npm 仍在 $K_AGENT_HOME/cache/runtime。
    runtime = (runtime or ensure_shared_runtime()).resolve()
    link = target_dir / ".runtime"
    # 断链时 exists() 为假。先认 symlink/junction，避免 is_dir() 跟着链接走进共享 cache。
    if _is_dir_link(link):
        try:
            if link.resolve() == runtime:
                return link
        except OSError:
            # 断链/无权限：当过期链接处理，下面只拆链接本身。
            pass
        _remove_path(link)
    elif link.exists():
        _remove_path(link)
    target_dir.mkdir(parents=True, exist_ok=True)
    _link_directory(link, runtime)
    return link


def shared_runtime_prefix(
    target_dir: Path | None = None, runtime: Path | None = None
) -> Path:
    """子进程应使用的 runtime 前缀：有工作区则是 `.runtime` 链接，否则是 cache 实路径。"""

    runtime = (runtime or ensure_shared_runtime()).resolve()
    if target_dir is None:
        return runtime
    return link_shared_runtime(target_dir, runtime)


def _env_runtime_root(runtime: Path) -> Path:
    """把 runtime 做成绝对路径，但绝不跟随符号链接。

    PATH/npm 需要绝对前缀；`Path.resolve()` 会穿过 workspace `.runtime`
    回到 `$K_AGENT_HOME/cache/runtime`，等于白挂链接。
    """

    # cwd=/proj 时：
    #   ~/.runtime          → /Users/me/.runtime（仍不跟随链接）
    #   workspace/.runtime  → /proj/workspace/.runtime
    #   /abs/ws/.runtime    → 原样返回（不会变成 cache/runtime）
    runtime = runtime.expanduser()  # 只把开头的 ~ 换成当前用户 home，例如 ~/x → /Users/me/x
    if not runtime.is_absolute():
        runtime = Path.cwd() / runtime
    return runtime


def shared_runtime_tool_env(runtime: Path | None = None) -> dict[str, str]:
    """注入 Bash/CLI 子进程的 PATH/npm prefix，使安装复用同一项目前缀。"""

    runtime = (
        ensure_shared_runtime()
        if runtime is None
        else _env_runtime_root(Path(runtime))
    )
    node_bin = str(runtime / "node" / "bin")
    parent_path = os.environ.get("PATH", "")
    return {
        # 给 Agent/提示词用的 runtime 根：工作区里通常是 .runtime 链接。
        "K_AGENT_SHARED_RUNTIME": str(runtime),
        # Team 任务约定交付目录名（相对 cwd），不是绝对路径。
        "K_AGENT_TASK_OUTPUT": "output",
        # npm 下载缓存，避免每次 install 打到用户 ~/.npm。
        "NPM_CONFIG_CACHE": str(runtime / "npm-cache"),
        # 全局包装到 runtime/node（bin 在 node/bin），而不是用户 ~/.npm-global。
        "npm_config_prefix": str(runtime / "node"),
        # Unix PATH 用冒号拼接目录。例：
        # node_bin=/proj/ws/.runtime/node/bin
        # parent_path=/usr/bin:/bin
        # → /proj/ws/.runtime/node/bin:/usr/bin:/bin（共享 bin 优先）
        "PATH": f"{node_bin}:{parent_path}" if parent_path else node_bin,
    }


def session_bundle_dir(session_id: str) -> Path:
    """单会话目录：会话 JSON + workspace。"""

    return sessions_dir() / session_id


def session_json_path(session_id: str) -> Path:
    return session_bundle_dir(session_id) / f"{session_id}.json"


def session_workspace_dir(session_id: str) -> Path:
    """对话绑定的 CLI/本地工具 cwd（非 Team 任务目录）。"""

    return session_bundle_dir(session_id) / "workspace"


def team_task_dir(team_id: str, task_id: str) -> Path:
    """单个 Team 任务的持久文件包根目录。"""

    return teams_dir() / team_id / "tasks" / task_id


def team_task_output_dir(team_id: str, task_id: str) -> Path:
    """任务交付物目录；通常作为分配给该任务 Agent 的 cwd。"""

    return team_task_dir(team_id, task_id) / "output"


def memory_dir() -> Path:
    return content_dir() / "memory"


def skills_dir() -> Path:
    return content_dir() / "skills"


def mcp_config_path() -> Path:
    return config_dir() / "mcp.json"


def user_mcp_config_path() -> Path:
    return config_dir() / "user-mcp.json"


def models_config_path() -> Path:
    return config_dir() / "models.json"


def permissions_path() -> Path:
    return config_dir() / "permissions.json"


def mcp_catalog_path() -> Path:
    return catalog_dir() / "mcp.json"


def skills_catalog_path() -> Path:
    return catalog_dir() / "skills.json"


def ensure_home_layout(*, migrate: bool = True) -> Path:
    """创建 `$K_AGENT_HOME` 下 config/state/content/cache 目录树。

    `migrate` 只为旧调用保留；不再把遗留 data/ 拷进 home。
    """

    del migrate

    home = agent_home()
    for path in (
        config_dir(),
        catalog_dir(),
        sessions_dir(),
        teams_dir(),
        memory_dir(),
        skills_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
    ensure_shared_runtime()
    return home


def display_home() -> str:
    """UI/文档用短路径；落在用户 home 下时优先写成 `~/…`。"""

    home = agent_home()
    try:
        return f"~/{home.relative_to(Path.home())}"
    except ValueError:
        return str(home)


def resolve_managed_path(value: str | Path) -> Path:
    """解析入库路径：相对值一律相对 `$K_AGENT_HOME`，而非进程 cwd。"""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = agent_home() / path
    return path.resolve()


def to_managed_path(value: str | Path) -> str:
    """持久化/API 优先返回相对 home 的路径；越界工作区仍给绝对路径。"""

    resolved = resolve_managed_path(value)
    try:
        return resolved.relative_to(agent_home().resolve()).as_posix()
    except ValueError:
        # Custom workspaces outside the agent home still need an absolute path.
        return str(resolved)


def public_home_relative_path(value: str | Path | None) -> str | None:
    """对外展示路径：相对 home → `~/…` → 否则 basename/绝对路径。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    resolved = Path(text).expanduser()
    if not resolved.is_absolute():
        return resolved.as_posix()
    resolved = resolved.resolve()
    try:
        return resolved.relative_to(agent_home().resolve()).as_posix()
    except ValueError:
        pass
    try:
        return f"~/{resolved.relative_to(Path.home().resolve()).as_posix()}"
    except ValueError:
        # Custom workspaces outside both homes have no safe relative form.
        return str(resolved)
