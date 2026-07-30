"""Per-session bookkeeping for memory files loaded into agent prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock


@dataclass
class MemorySessionState:
    """Memory paths already loaded plus paths queued to trigger a refresh."""

    loaded_paths: set[str] = field(default_factory=set)
    trigger_paths: set[str] = field(default_factory=set)

    def mark_loaded(self, paths: list[str]) -> None:
        """记录本会话已经加载过的 memory 路径。"""
        self.loaded_paths.update(paths)

    def queue_triggers(self, paths: list[Path]) -> None:
        """把路径加入下一轮 memory 触发队列。"""
        self.trigger_paths.update(str(path.expanduser()) for path in paths)

    def consume_triggers(self) -> list[Path]:
        """取出并清空待触发的 memory 路径。"""
        paths = [Path(path) for path in sorted(self.trigger_paths)]
        self.trigger_paths.clear()
        return paths


class MemorySessionRegistry:
    """Process-local memory injection state keyed by chat session."""

    def __init__(self) -> None:
        """初始化对象依赖和内部状态。"""
        self._lock = RLock()
        self._states: dict[str, MemorySessionState] = {}

    def get(self, session_id: str) -> MemorySessionState:
        """读取或创建当前对象管理的条目。"""
        with self._lock:
            return self._states.setdefault(session_id, MemorySessionState())

    def clear(self, session_id: str | None = None) -> None:
        """清空当前对象维护的缓存或会话状态。"""
        with self._lock:
            if session_id is None:
                self._states.clear()
            else:
                self._states.pop(session_id, None)


MEMORY_SESSIONS = MemorySessionRegistry()
