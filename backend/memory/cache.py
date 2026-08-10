"""按路径 mtime 失效的 memory 解析结果缓存。

pipeline：`memory.discovery` 读写本缓存；文件变更由 watcher →
`reset_prompt_caches` → `clear_memory_cache` 统一清空。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from backend.memory.models import MemoryFile


@dataclass(frozen=True)
class MemoryCacheEntry:
    """一条缓存：已解析的 MemoryFile 列表，及其关联路径的 mtime 签名。"""
    files: tuple[MemoryFile, ...]
    mtimes: tuple[tuple[str, float | None], ...]


class MemoryCache:
    """进程内 memory 加载结果缓存；任一关联路径 mtime 变化即判失效。"""
    def __init__(self) -> None:
        """准备线程安全的条目表（RLock + dict）。"""
        self._lock = RLock()
        self._entries: dict[str, MemoryCacheEntry] = {}

    def get(self, key: str) -> list[MemoryFile] | None:
        """按 key 取缓存；缺失或 mtime 过期则剔除并返回 None。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not self._is_fresh(entry):
                self._entries.pop(key, None)
                return None
            return list(entry.files)

    def set(self, key: str, files: list[MemoryFile]) -> None:
        """写入解析结果，并把主文件与 include 路径的 mtime 一并记录。"""
        with self._lock:
            watched = {str(item.path): _mtime(item.path) for item in files}
            # include 进来的文件同样要纳入失效判断：只改被引用文件时，
            # 主文件的 mtime 不变，不记录它就会一直返回过期内容。
            for item in files:
                for include in item.includes:
                    watched[str(include)] = _mtime(include)
            self._entries[key] = MemoryCacheEntry(tuple(files), tuple(sorted(watched.items())))

    def clear(self) -> None:
        """清空全部 memory 缓存条目。"""
        with self._lock:
            self._entries.clear()

    def _is_fresh(self, entry: MemoryCacheEntry) -> bool:
        """条目记录的路径 mtime 是否仍与磁盘一致。"""
        # 文件被删除时 _mtime 返回 None，与记录值不等，同样判为失效。
        return all(_mtime(Path(path)) == mtime for path, mtime in entry.mtimes)


MEMORY_CACHE = MemoryCache()


def clear_memory_cache() -> None:
    """清空全局 `MEMORY_CACHE`（供 prompt lifecycle / 测试调用）。"""
    MEMORY_CACHE.clear()


def _mtime(path: Path) -> float | None:
    """读取单路径 mtime；不存在或不可读时返回 None。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None
