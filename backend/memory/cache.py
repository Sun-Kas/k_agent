"""Modification-time-aware cache for parsed memory files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from backend.memory.models import MemoryFile


@dataclass(frozen=True)
class MemoryCacheEntry:
    """保存 memory 缓存内容和关联路径修改时间。"""
    files: tuple[MemoryFile, ...]
    mtimes: tuple[tuple[str, float | None], ...]


class MemoryCache:
    """缓存 memory 加载结果并根据 mtime 失效。"""
    def __init__(self) -> None:
        """初始化对象依赖和内部状态。"""
        self._lock = RLock()
        self._entries: dict[str, MemoryCacheEntry] = {}

    def get(self, key: str) -> list[MemoryFile] | None:
        """读取或创建当前对象管理的条目。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not self._is_fresh(entry):
                self._entries.pop(key, None)
                return None
            return list(entry.files)

    def set(self, key: str, files: list[MemoryFile]) -> None:
        """写入当前对象维护的缓存条目。"""
        with self._lock:
            watched = {str(item.path): _mtime(item.path) for item in files}
            for item in files:
                for include in item.includes:
                    watched[str(include)] = _mtime(include)
            self._entries[key] = MemoryCacheEntry(tuple(files), tuple(sorted(watched.items())))

    def clear(self) -> None:
        """清空当前对象维护的缓存或会话状态。"""
        with self._lock:
            self._entries.clear()

    def _is_fresh(self, entry: MemoryCacheEntry) -> bool:
        """判断 memory 缓存是否仍与文件 mtime 一致。"""
        return all(_mtime(Path(path)) == mtime for path, mtime in entry.mtimes)


MEMORY_CACHE = MemoryCache()


def clear_memory_cache() -> None:
    """清空全局 memory 缓存。"""
    MEMORY_CACHE.clear()


def _mtime(path: Path) -> float | None:
    """读取文件或目录树的最新修改时间。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None
