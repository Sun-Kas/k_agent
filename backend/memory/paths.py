"""Canonical path helpers for project-local automatic memory storage."""

from __future__ import annotations

import os
from pathlib import Path

from backend.home import memory_dir


def auto_memory_dir(cwd: Path | None = None) -> Path:
    """返回个人 memory 目录（默认 `$K_AGENT_HOME/content/memory`）。"""

    del cwd  # Kept for call-site compatibility; memory is home-scoped now.
    override = os.getenv("K_AGENT_MEMORY_PATH_OVERRIDE")
    if override:
        return Path(override).expanduser().resolve()
    base = os.getenv("K_AGENT_MEMORY_BASE_DIR")
    if base:
        return Path(base).expanduser().resolve() / "memory"
    return memory_dir()
