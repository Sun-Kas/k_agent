"""Single layout for durable agent state under `$K_AGENT_HOME`.

Default home is `~/.k_agent`. Set `K_AGENT_HOME` (relative paths resolve against
the project root) for project-local development. Layout:

```
$K_AGENT_HOME/
  config/
    mcp.json                 # managed MCP connection config
    user-mcp.json            # optional user MCP overlay
    models.json
    permissions.json
    catalog/
      mcp.json               # frontend MCP picker summaries
      skills.json            # frontend Skill picker summaries
  cache/
    runtime/                 # project-wide shared Node/npm tooling (all sessions + teams)
      node/                  # npm --prefix for global CLIs
      npm-cache/
      projects/              # shared package installs reused across the whole home
  state/
    sessions/                # FileStorage session root
      {session_id}/
        {session_id}.json    # conversation + AG-UI log
        workspace/           # per-session cwd for CLI agents / tools
          .runtime -> $K_AGENT_HOME/cache/runtime
    teams/
      team_runtime.db        # durable Team control plane
      {team_id}/
        workspace/           # default Team workspace; accepted deliverables only
          artifacts/{task_id}/{artifact_id}/
        tasks/{task_id}/
          manifest.json      # task/run/file lineage
          input/             # task, mailbox, dependency Artifact metadata
          output/            # deliverables only (Agent cwd is the task dir)
          .runtime -> $K_AGENT_HOME/cache/runtime
          artifacts/         # readable copies of submitted Artifact text
          logs/              # raw provider event stream by run
  content/
    memory/                  # durable MEMORY.md
    skills/                  # installed Skill packages
```

Legacy repo paths (`data/…`, `backend/config/runtime/…`) are copied into an empty
home on first start; originals are left untouched.
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
    """Drop cached home resolution (tests / reconfiguration)."""

    global _home_cache, _migrated
    _home_cache = None
    _migrated = False


def agent_home() -> Path:
    """Resolve `$K_AGENT_HOME`, defaulting to `~/.k_agent`."""

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
    """Durable Team Runtime metadata, artifacts, and isolated workspaces."""

    return state_dir() / "teams"


def shared_runtime_dir() -> Path:
    """Project-wide Node/npm tooling shared by every session and Team task."""

    return agent_home() / "cache" / "runtime"


def ensure_shared_runtime() -> Path:
    """Create the shared runtime layout used for CLI prefixes and npm caches."""

    runtime = shared_runtime_dir().resolve()
    (runtime / "npm-cache").mkdir(parents=True, exist_ok=True)
    (runtime / "node").mkdir(parents=True, exist_ok=True)
    (runtime / "projects").mkdir(parents=True, exist_ok=True)
    return runtime


def link_shared_runtime(target_dir: Path, runtime: Path | None = None) -> Path:
    """Expose the project shared runtime inside a workspace as `.runtime`."""

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
    """Env injected into Bash/CLI children so installs reuse one project prefix."""

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
    """Directory holding one session's JSON record and workspace."""

    return sessions_dir() / session_id


def session_json_path(session_id: str) -> Path:
    return session_bundle_dir(session_id) / f"{session_id}.json"


def session_workspace_dir(session_id: str) -> Path:
    """Working directory for CLI agents bound to this conversation."""

    return session_bundle_dir(session_id) / "workspace"


def team_workspace_dir(team_id: str, agent_id: str) -> Path:
    """Return the legacy Agent-owned Team workspace path.

    New Team runs use task-scoped directories instead. Keep this resolver for
    old snapshots and callers that need to inspect pre-migration workspaces.
    """

    return teams_dir() / team_id / "workspaces" / agent_id


def team_task_dir(team_id: str, task_id: str) -> Path:
    """Return the durable file bundle owned by one Team task."""

    return teams_dir() / team_id / "tasks" / task_id


def team_task_output_dir(team_id: str, task_id: str) -> Path:
    """Return the task-local cwd exposed to its assigned Agent."""

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
    """Create the home tree and optionally import legacy project data."""

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
    """Copy known legacy locations into an empty home slot. Idempotent."""

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
    """Copy source into destination only when destination is still unused."""

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
    """Short path string for UI/docs (prefer ~ when under the user home)."""

    home = agent_home()
    try:
        return f"~/{home.relative_to(Path.home())}"
    except ValueError:
        return str(home)


def resolve_managed_path(value: str | Path) -> Path:
    """Resolve a stored workspace path; relative values are under `$K_AGENT_HOME`."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = agent_home() / path
    return path.resolve()


def to_managed_path(value: str | Path) -> str:
    """Prefer a `$K_AGENT_HOME`-relative path for persistence and API responses."""

    resolved = resolve_managed_path(value)
    try:
        return resolved.relative_to(agent_home().resolve()).as_posix()
    except ValueError:
        # Custom workspaces outside the agent home still need an absolute path.
        return str(resolved)


def public_home_relative_path(value: str | Path | None) -> str | None:
    """Return a path suitable for API/UI: relative to home, else `~/…`, else basename."""

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
