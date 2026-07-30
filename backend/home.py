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
  state/
    sessions/                # conversation history (FileStorage root)
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
        memory_dir(),
        skills_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
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
