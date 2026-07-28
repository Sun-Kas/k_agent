from __future__ import annotations

from backend.memory.models import MemoryFile


def summarize_memory_load(prefix: str, files: list[MemoryFile]) -> str:
    by_type: dict[str, int] = {}
    for file in files:
        by_type[file.type.value] = by_type.get(file.type.value, 0) + 1
    details = ", ".join(f"{key}={value}" for key, value in sorted(by_type.items())) or "none"
    return f"memory:{prefix}:{len(files)} files ({details})"

