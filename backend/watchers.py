from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class WatchedPathState:
    mtimes: dict[str, float | None] = field(default_factory=dict)


class PollingChangeWatcher:
    """Small polling watcher for prompt-affecting files.

    Claude Code uses richer app/runtime hooks. In this backend we avoid adding a
    native filesystem dependency and instead poll a tiny set of config/memory
    paths. The watcher is conservative: any observed change clears caches.
    """

    def __init__(self, roots: list[Path], on_change: Callable[[str], None], interval_seconds: float = 1.5):
        self.roots = roots
        self.on_change = on_change
        self.interval_seconds = interval_seconds
        self.state = WatchedPathState()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None

    async def _run(self) -> None:
        self.state.mtimes = self._snapshot()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                current = self._snapshot()
                if current != self.state.mtimes:
                    self.state.mtimes = current
                    self.on_change("watched_files_changed")

    def _snapshot(self) -> dict[str, float | None]:
        watched: dict[str, float | None] = {}
        for root in self.roots:
            if root.is_file():
                watched[str(root)] = _mtime(root)
                continue
            if not root.exists():
                watched[str(root)] = None
                continue
            for path in root.rglob("*"):
                if path.is_file() and _interesting(path):
                    watched[str(path)] = _mtime(path)
        return watched


def _interesting(path: Path) -> bool:
    return path.name in {"K_AGENT.md", "CLAUDE.md", "K_AGENT.local.md", "CLAUDE.local.md", "SKILL.md"} or path.suffix == ".json"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None
