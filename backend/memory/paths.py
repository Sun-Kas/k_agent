from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]


def auto_memory_dir(cwd: Path) -> Path:
    override = os.getenv("K_AGENT_MEMORY_PATH_OVERRIDE")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.getenv("K_AGENT_MEMORY_BASE_DIR", PROJECT_DIR / "data")).expanduser().resolve() / "memory"
