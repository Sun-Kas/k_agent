"""Polling watcher used to invalidate runtime state when configuration files change."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class WatchedPathState:
    """保存一个监听路径的最近修改时间。"""
    mtimes: dict[str, float | None] = field(default_factory=dict)


class PollingChangeWatcher:
    """Small polling watcher for prompt-affecting files.

    Claude Code uses richer app/runtime hooks. In this backend we avoid adding a
    native filesystem dependency and instead poll a tiny set of config/memory
    paths. The watcher is conservative: any observed change clears caches.
    """

    def __init__(self, roots: list[Path], on_change: Callable[[str], None], interval_seconds: float = 1.5):
        """初始化对象依赖和内部状态。"""
        self.roots = roots
        self.on_change = on_change
        self.interval_seconds = interval_seconds
        self.state = WatchedPathState()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """启动后台轮询监听任务。"""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """停止后台轮询监听任务。"""
        self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None

    async def _run(self) -> None:
        """启动 MCP stdio 进程并建立底层 ClientSession。"""
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
        """采集当前监听路径的修改时间快照。"""
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
    """判断文件是否属于需要监听的类型。"""
    return path.name in {"CLAUDE.md", "CLAUDE.local.md", "SKILL.md"} or path.suffix in {".md", ".json"}


def _mtime(path: Path) -> float | None:
    """读取文件或目录树的最新修改时间。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None
