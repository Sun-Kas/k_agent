from __future__ import annotations

from backend.memory.constants import MAX_MEMORY_CHARACTER_COUNT, MEMORY_INSTRUCTION_PROMPT
from backend.memory.models import MemoryFile, MemoryType


MEMORY_TYPE_BUDGET_WEIGHTS = {
    MemoryType.MANAGED: 1,
    MemoryType.USER: 2,
    MemoryType.PROJECT: 4,
    MemoryType.LOCAL: 5,
    MemoryType.AUTOMATED: 3,
    MemoryType.TEAM: 3,
}


def get_memory_context(
    memory_files: list[MemoryFile],
    *,
    max_chars: int = MAX_MEMORY_CHARACTER_COUNT,
) -> str:
    blocks: list[str] = []
    total_chars = 0
    for memory_file in _budgeted_files(memory_files, max_chars=max_chars):
        content = memory_file.content.strip()
        if not content:
            continue
        remaining = max_chars - total_chars
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[:remaining].rstrip() + "\n\n[Memory truncated due to context limit.]"
        total_chars += len(content)
        blocks.append(f"Contents of {memory_file.path}{_description(memory_file.type)}:\n\n{content}")
    if not blocks:
        return ""
    return f"{MEMORY_INSTRUCTION_PROMPT}\n\n" + "\n\n".join(blocks)


def _budgeted_files(memory_files: list[MemoryFile], *, max_chars: int) -> list[MemoryFile]:
    """Keep broad-to-local order while protecting high-priority memory from truncation."""
    if sum(len(item.content) for item in memory_files) <= max_chars:
        return memory_files
    weighted = sorted(
        enumerate(memory_files),
        key=lambda pair: (MEMORY_TYPE_BUDGET_WEIGHTS.get(pair[1].type, 1), pair[0]),
        reverse=True,
    )
    selected: set[int] = set()
    used = 0
    soft_budget = max_chars * 0.9
    for index, memory_file in weighted:
        size = len(memory_file.content)
        if used + size <= soft_budget or not selected:
            selected.add(index)
            used += size
    return [memory_file for index, memory_file in enumerate(memory_files) if index in selected]


def _description(memory_type: MemoryType) -> str:
    if memory_type == MemoryType.PROJECT:
        return " (workspace instructions, shared with this project)"
    if memory_type == MemoryType.LOCAL:
        return " (user's private project instructions, not checked in)"
    if memory_type == MemoryType.TEAM:
        return " (shared team memory, synced across the organization)"
    if memory_type == MemoryType.AUTOMATED:
        return " (user's auto-memory, persists across conversations)"
    if memory_type == MemoryType.MANAGED:
        return " (managed global instructions for all projects)"
    return " (user's private global instructions for all projects)"
