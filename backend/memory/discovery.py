"""Discover applicable memory files and report policy or parsing failures."""

from __future__ import annotations

from pathlib import Path

from backend.memory.cache import MEMORY_CACHE
from backend.memory.constants import (
    LOCAL_MEMORY_FILENAMES,
    MAX_INCLUDE_DEPTH,
    MEMORY_DIR_NAMES,
    MEMORY_FILENAMES,
)
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
    """发现当前工作区可用的 memory 文件。"""
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
    """读取 memory 文件并返回加载报告。"""
    cwd = (cwd or Path.cwd()).resolve()
    policy = policy_from_env(include_external=include_external)
    cache_key = f"eager:{cwd}:{include_external}:{additional_directories}:{policy}"
    if use_cache:
        cached = MEMORY_CACHE.get(cache_key)
        if cached is not None:
            return MemoryLoadReport(files=cached)

    report = MemoryLoadReport()
    processed: set[Path] = set()

    if policy.allow_user_memory:
        _append_memory(
            report,
            Path.home() / ".claude" / "CLAUDE.md",
            MemoryType.USER,
            processed,
            policy=policy,
        )

    if policy.allow_project_memory:
        for directory in reversed([cwd, *cwd.parents]):
            for filename in MEMORY_FILENAMES:
                _append_memory(
                    report,
                    directory / filename,
                    MemoryType.PROJECT,
                    processed,
                    policy=policy,
                )
            for memory_dir, filename in zip(MEMORY_DIR_NAMES, MEMORY_FILENAMES):
                _append_memory(
                    report,
                    directory / memory_dir / filename,
                    MemoryType.PROJECT,
                    processed,
                    policy=policy,
                )
            rules_dir = directory / ".claude" / "rules"
            for rule in _rule_files(rules_dir):
                memory = _read_memory(rule, MemoryType.PROJECT, policy)
                if memory is not None and not memory.globs:
                    _append_memory(
                        report,
                        rule,
                        MemoryType.PROJECT,
                        processed,
                        policy=policy,
                    )

    if policy.allow_local_memory:
        for directory in reversed([cwd, *cwd.parents]):
            for filename in LOCAL_MEMORY_FILENAMES:
                _append_memory(
                    report,
                    directory / filename,
                    MemoryType.LOCAL,
                    processed,
                    policy=policy,
                )

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
    """根据引用路径发现嵌套 memory 文件。"""
    cwd = (cwd or Path.cwd()).resolve()
    target = target_path.expanduser().resolve()
    policy = policy_from_env()
    report = MemoryLoadReport()
    processed: set[Path] = set()

    target_dir = target if target.is_dir() else target.parent
    directories: list[Path] = []
    current = target_dir
    while current != cwd and _is_relative_to(current, cwd):
        directories.append(current)
        if current.parent == current:
            break
        current = current.parent
    directories.reverse()

    for directory in directories:
        if policy.allow_project_memory:
            for filename in MEMORY_FILENAMES:
                _append_memory(report, directory / filename, MemoryType.PROJECT, processed, policy=policy)
            for memory_dir, filename in zip(MEMORY_DIR_NAMES, MEMORY_FILENAMES):
                _append_memory(
                    report,
                    directory / memory_dir / filename,
                    MemoryType.PROJECT,
                    processed,
                    policy=policy,
                )
        if policy.allow_local_memory:
            for filename in LOCAL_MEMORY_FILENAMES:
                _append_memory(report, directory / filename, MemoryType.LOCAL, processed, policy=policy)

    if policy.allow_project_memory:
        for directory in reversed([cwd, *cwd.parents]):
            rules_dir = directory / ".claude" / "rules"
            for rule in _rule_files(rules_dir):
                memory = _read_memory(rule, MemoryType.PROJECT, policy)
                if memory is not None and memory.globs and any(
                    _matches_glob(target, cwd, glob) for glob in memory.globs
                ):
                    _append_memory(report, rule, MemoryType.PROJECT, processed, policy=policy)
    return report.files


def is_memory_file(path: Path) -> bool:
    """判断路径是否是项目支持的 memory 文件。"""
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
    """按策略把 memory 文件追加进加载结果。"""
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


def _read_memory(path: Path, memory_type: MemoryType, policy: MemoryPolicy) -> MemoryFile | None:
    """安全读取并解析单个 memory 文件。"""
    report = MemoryLoadReport()
    _append_memory(report, path, memory_type, set(), policy=policy)
    return report.files[0] if report.files else None


def _rule_files(directory: Path) -> list[Path]:
    """列出目录中的 memory 规则文件。"""
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*.md") if path.is_file())


def _matches_glob(target: Path, cwd: Path, pattern: str) -> bool:
    """判断路径是否匹配 memory glob 规则。"""
    try:
        relative = target.relative_to(cwd)
    except ValueError:
        relative = target
    return relative.match(pattern) or target.match(pattern)


def _is_relative_to(path: Path, parent: Path) -> bool:
    """判断一个路径是否位于另一个路径之下。"""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
