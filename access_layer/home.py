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

首次启动可把遗留 `data/…`、`backend/config/runtime/…` 拷入空 home；原件不动。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[1]
LEGACY_DATA_DIR = PROJECT_DIR / "data"
LEGACY_RUNTIME_DIR = PROJECT_DIR / "backend" / "config" / "runtime"

_home_cache: Path | None = None
_migrated = False


def reset_home_cache() -> None:
    """测试/重配：丢弃缓存的 home 解析与迁移标志。"""

    global _home_cache, _migrated
    _home_cache = None
    _migrated = False


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


def link_shared_runtime(target_dir: Path, runtime: Path | None = None) -> Path:
    """在工作区挂 `.runtime` → 共享 runtime；旧链接/目录先拆再建。"""

    runtime = (runtime or ensure_shared_runtime()).resolve()
    link = target_dir / ".runtime"
    if link.is_symlink():
        try:
            if link.resolve() == runtime:
                return link
        except OSError:
            pass
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()
    target_dir.mkdir(parents=True, exist_ok=True)
    link.symlink_to(runtime, target_is_directory=True)
    return link


def shared_runtime_tool_env(runtime: Path | None = None) -> dict[str, str]:
    """注入 Bash/CLI 子进程的 PATH/npm prefix，使安装复用同一项目前缀。"""

    runtime = (runtime or ensure_shared_runtime()).resolve()
    node_bin = str(runtime / "node" / "bin")
    parent_path = os.environ.get("PATH", "")
    return {
        "K_AGENT_SHARED_RUNTIME": str(runtime),
        "K_AGENT_TASK_OUTPUT": "output",
        "NPM_CONFIG_CACHE": str(runtime / "npm-cache"),
        "npm_config_prefix": str(runtime / "node"),
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


def team_workspace_dir(team_id: str, agent_id: str) -> Path:
    """遗留的 Agent 级 Team 工作区路径。

    新跑法用任务级目录；保留本解析器供旧快照与迁移前巡检。
    """

    return teams_dir() / team_id / "workspaces" / agent_id


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
    """创建 home 目录树；可选把遗留项目数据迁入空槽位。"""

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
    if migrate:
        migrate_legacy_home()
    return home


def migrate_legacy_home() -> list[str]:
    """幂等：把已知遗留路径拷进尚未占用的 home 槽位。"""

    global _migrated
    if _migrated:
        return []
    _migrated = True
    actions: list[str] = []

    copies: list[tuple[Path, Path, bool]] = [
        # (source, destination, is_directory)
        (LEGACY_DATA_DIR / "sessions", sessions_dir(), True),
        (LEGACY_DATA_DIR / "memory", memory_dir(), True),
        (LEGACY_DATA_DIR / "skill", skills_dir(), True),
        (LEGACY_DATA_DIR / "mcp.json", mcp_catalog_path(), False),
        (LEGACY_DATA_DIR / "skill.json", skills_catalog_path(), False),
        (LEGACY_RUNTIME_DIR / "mcp.config.json", mcp_config_path(), False),
        (LEGACY_RUNTIME_DIR / "models.config.json", models_config_path(), False),
        (Path.home() / ".k_agent" / "permissions.json", permissions_path(), False),
        (Path.home() / ".k_agent" / "mcp.json", user_mcp_config_path(), False),
    ]
    for source, destination, is_dir in copies:
        moved = _copy_if_needed(source, destination, is_directory=is_dir)
        if moved:
            actions.append(f"{source} -> {destination}")

    if actions:
        logger.info(
            "Migrated legacy agent data into %s (%d items)",
            agent_home(),
            len(actions),
        )
    return actions


def _copy_if_needed(source: Path, destination: Path, *, is_directory: bool) -> bool:
    """仅当目标仍空/不存在时拷贝；同源同目标直接跳过。"""

    try:
        if not source.exists():
            return False
        # Never pull the live home onto itself when K_AGENT_HOME=~/.k_agent and
        # the legacy file already sits at the new path.
        if source.resolve() == destination.resolve():
            return False
        if is_directory:
            if any(destination.iterdir()) if destination.is_dir() else destination.exists():
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination, dirs_exist_ok=True)
            return True
        if destination.exists():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return True
    except OSError as exc:
        logger.info("Skipped legacy migrate %s -> %s: %s", source, destination, exc)
        return False


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
