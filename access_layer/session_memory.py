from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock


@dataclass
class MemorySessionState:
    loaded_paths: set[str] = field(default_factory=set)
    trigger_paths: set[str] = field(default_factory=set)

    def mark_loaded(self, paths: list[str]) -> None:
        self.loaded_paths.update(paths)

    def queue_triggers(self, paths: list[Path]) -> None:
        self.trigger_paths.update(str(path.expanduser()) for path in paths)

    def consume_triggers(self) -> list[Path]:
        paths = [Path(path) for path in sorted(self.trigger_paths)]
        self.trigger_paths.clear()
        return paths


class MemorySessionRegistry:
    """Process-local memory injection state keyed by chat session."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._states: dict[str, MemorySessionState] = {}

    def get(self, session_id: str) -> MemorySessionState:
        with self._lock:
            return self._states.setdefault(session_id, MemorySessionState())

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._states.clear()
            else:
                self._states.pop(session_id, None)


MEMORY_SESSIONS = MemorySessionRegistry()
