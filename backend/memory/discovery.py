from __future__ import annotations

from pathlib import Path

from backend.memory.cache import MEMORY_CACHE
from backend.memory.constants import MAX_INCLUDE_DEPTH, MEMORY_FILENAMES
from backend.memory.models import MemoryFile, MemoryLoadReport, MemoryType
from backend.memory.parser import parse_memory, resolve_include
from backend.memory.paths import auto_memory_dir
from backend.memory.policy import MemoryPolicy, can_include_path, can_read_memory_path, policy_from_env
from backend.storage.file import FileStorage


FILE_STORAGE = FileStorage("/")


def get_memory_files(
    cwd: Path | None = None,
    *,
    additional_directories: list[Path] | None = None,
    include_external: bool = False,
    use_cache: bool = True,
) -> list[MemoryFile]:
    report = load_memory_report(
        cwd,
        additional_directories=additional_directories,
        include_external=include_external,
        use_cache=use_cache,
    )
    return report.files


def load_memory_report(
    cwd: Path | None = None,
    *,
    additional_directories: list[Path] | None = None,
    include_external: bool = False,
    use_cache: bool = True,
) -> MemoryLoadReport:
    cwd = (cwd or Path.cwd()).resolve()
    policy = policy_from_env(include_external=include_external)
    cache_key = f"eager:{cwd}:{include_external}:{additional_directories}:{policy}"
    if use_cache:
        cached = MEMORY_CACHE.get(cache_key)
        if cached is not None:
            return MemoryLoadReport(files=cached)

    report = MemoryLoadReport()
    processed: set[Path] = set()

    if policy.allow_auto_memory:
        _append_memory(
            report,
            auto_memory_dir(cwd) / "MEMORY.md",
            MemoryType.AUTOMATED,
            processed,
            policy=MemoryPolicy(include_external=True),
        )

    if use_cache:
        MEMORY_CACHE.set(cache_key, report.files)
    return report


def get_nested_memory_files(target_path: Path, cwd: Path | None = None) -> list[MemoryFile]:
    return []


def is_memory_file(path: Path) -> bool:
    path = path.expanduser()
    name = path.name
    if name in MEMORY_FILENAMES or name == "MEMORY.md":
        return True
    return False


def _append_memory(
    report: MemoryLoadReport,
    path: Path,
    memory_type: MemoryType,
    processed: set[Path],
    *,
    policy: MemoryPolicy,
    depth: int = 0,
    include_base: Path | None = None,
) -> None:
    path = path.expanduser()
    raw = FILE_STORAGE.read_text_sync(str(path))
    if raw is None:
        return
    resolved = path.resolve()
    if resolved in processed:
        return
    if depth > MAX_INCLUDE_DEPTH:
        report.warnings.append(f"include depth exceeded: {resolved}")
        return
    allowed, reason = can_read_memory_path(resolved)
    if not allowed:
        report.skipped.append(reason or str(resolved))
        return
    if include_base is not None:
        allowed, reason = can_include_path(resolved, include_base, policy)
        if not allowed:
            report.skipped.append(reason or str(resolved))
            return

    processed.add(resolved)
    parsed = parse_memory(raw, memory_type)
    include_paths = tuple(resolve_include(item, resolved.parent).expanduser().resolve() for item in parsed.includes)
    report.files.append(
        MemoryFile(
            path=resolved,
            content=parsed.content,
            type=memory_type,
            raw_content=raw,
            includes=include_paths,
            globs=parsed.globs,
            content_differs_from_disk=parsed.content_differs_from_disk,
        )
    )

    base = include_base or resolved.parent
    for include_path in include_paths:
        _append_memory(report, include_path, memory_type, processed, policy=policy, depth=depth + 1, include_base=base)
