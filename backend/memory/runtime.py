"""Request-local eager and lazy memory loading helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

from backend.api.schemas import ChatMessage
from backend.memory.discovery import get_memory_files, get_nested_memory_files
from backend.memory.models import MemoryFile


_TOOL_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path",),
    "Grep": ("path",),
    "LS": ("path",),
}
_PATH_TOKEN = re.compile(r"(?:^|[\s'\"`(])((?:/|\.\.?/)?[\w.@+-]+(?:/[\w.@+ -]+)+)")


def resolve_instruction_root(raw: str | Path) -> Path:
    """Resolve the project rule root independently from session output state."""

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def load_eager_memory(
    instruction_root: Path,
    messages: Iterable[ChatMessage],
) -> list[MemoryFile]:
    """Load root rules plus nested rules explicitly referenced by recent user turns."""

    if _memory_disabled():
        return []
    files = list(get_memory_files(instruction_root))
    for path in extract_referenced_paths(messages, instruction_root):
        files.extend(get_nested_memory_files(path, instruction_root))
    return _dedupe(files)


def extract_referenced_paths(
    messages: Iterable[ChatMessage],
    instruction_root: Path,
) -> list[Path]:
    """Extract user-authored paths only; tool output is never trusted as a loader."""

    recent = list(messages)[-3:]
    paths: list[Path] = []
    for message in recent:
        if message.role != "user":
            continue
        for match in _PATH_TOKEN.findall(message.content or ""):
            resolved = _resolve_path(match.strip(), instruction_root)
            if resolved is not None:
                paths.append(resolved)
    return paths


def trusted_tool_paths(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    instruction_root: Path,
    tool_workspace: Path | None,
) -> list[Path]:
    """Accept only declared path fields from trusted local filesystem tools.

    Relative arguments resolve exactly as the tool does: against its request
    workspace. A session output path therefore cannot be reinterpreted as a
    project source path merely to trigger instruction loading.
    """

    fields = _TOOL_PATH_FIELDS.get(tool_name)
    if not fields or _memory_disabled():
        return []
    base = (tool_workspace or instruction_root).resolve()
    paths = []
    for field in fields:
        raw = arguments.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        resolved = _resolve_path(raw.strip(), base)
        if resolved is not None and _is_relative_to(resolved, instruction_root):
            paths.append(resolved)
    return paths


def load_fresh_nested_memory(
    paths: Iterable[Path],
    *,
    instruction_root: Path,
    loaded_paths: set[str],
) -> list[MemoryFile]:
    files: list[MemoryFile] = []
    for path in paths:
        files.extend(get_nested_memory_files(path, instruction_root))
    return [
        item
        for item in _dedupe(files)
        if str(item.path.resolve()) not in loaded_paths
    ]


def _resolve_path(raw: str, base: Path) -> Path | None:
    try:
        path = Path(raw).expanduser()
        return (path if path.is_absolute() else base / path).resolve()
    except (OSError, RuntimeError):
        return None


def _dedupe(files: Iterable[MemoryFile]) -> list[MemoryFile]:
    seen = set()
    result = []
    for item in files:
        path = item.path.resolve()
        if path in seen:
            continue
        seen.add(path)
        result.append(item)
    return result


def _memory_disabled() -> bool:
    value = os.getenv("K_AGENT_DISABLE_MEMORY") or os.getenv(
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS"
    )
    return bool(value and value.lower() not in {"", "0", "false", "no", "off"})


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
