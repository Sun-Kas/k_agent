from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from backend.memory.models import MemoryFile


@dataclass(frozen=True)
class MemoryCacheEntry:
    files: tuple[MemoryFile, ...]
    mtimes: tuple[tuple[str, float | None], ...]


class MemoryCache:
    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, MemoryCacheEntry] = {}

    def get(self, key: str) -> list[MemoryFile] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not self._is_fresh(entry):
                self._entries.pop(key, None)
                return None
            return list(entry.files)

    def set(self, key: str, files: list[MemoryFile]) -> None:
        with self._lock:
            watched = {str(item.path): _mtime(item.path) for item in files}
            for item in files:
                for include in item.includes:
                    watched[str(include)] = _mtime(include)
            self._entries[key] = MemoryCacheEntry(tuple(files), tuple(sorted(watched.items())))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _is_fresh(self, entry: MemoryCacheEntry) -> bool:
        return all(_mtime(Path(path)) == mtime for path, mtime in entry.mtimes)


MEMORY_CACHE = MemoryCache()


def clear_memory_cache() -> None:
    MEMORY_CACHE.clear()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None

