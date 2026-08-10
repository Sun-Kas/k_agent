"""轮询监听配置/记忆相关路径，发现变更后触发运行时缓存失效。

pipeline：Backend lifespan 启动 `PollingChangeWatcher`，回调接到
`reset_prompt_caches`；在下一次拼 prompt 前清空 section/memory 缓存。
不引入原生 FS 事件依赖，只轮询少量路径；任一变化即保守失效。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class WatchedPathState:
    """保存监听路径集合的最近 mtime 快照，供轮询对比。"""
    mtimes: dict[str, float | None] = field(default_factory=dict)


class PollingChangeWatcher:
    """对 prompt 相关文件做轻量轮询；相对原生 FS hook，依赖更少、行为更保守。"""

    def __init__(self, roots: list[Path], on_change: Callable[[str], None], interval_seconds: float = 1.5):
        """绑定监听根路径、变更回调与轮询间隔，并准备空快照状态。"""
        self.roots = roots
        self.on_change = on_change
        self.interval_seconds = interval_seconds
        self.state = WatchedPathState()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """启动后台轮询任务（幂等：已启动则忽略）。"""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """发出停止信号并等待轮询任务退出。"""
        self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None

    async def _run(self) -> None:
        """周期性对比 mtime 快照；有差异则调用一次 on_change。"""
        # 先取基线快照，避免把启动时的既有状态误判成一次变更。
        self.state.mtimes = self._snapshot()
        while not self._stop.is_set():
            # 用「等待停止信号并超时」代替 sleep：停止时能立刻退出，
            # 不必等完整的一个轮询周期。超时才说明该做一次检查。
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                current = self._snapshot()
                # 整体比较字典，能同时覆盖新增、删除和修改三种情况。
                if current != self.state.mtimes:
                    self.state.mtimes = current
                    self.on_change("watched_files_changed")

    def _snapshot(self) -> dict[str, float | None]:
        """遍历 roots，采集感兴趣文件的 path→mtime 映射。"""
        watched: dict[str, float | None] = {}
        for root in self.roots:
            if root.is_file():
                watched[str(root)] = _mtime(root)
                continue
            # 不存在的路径也要记成 None：这样它之后被创建出来时，
            # 快照会从 None 变成 mtime，同样能触发一次失效。
            if not root.exists():
                watched[str(root)] = None
                continue
            for path in root.rglob("*"):
                if path.is_file() and _interesting(path):
                    watched[str(path)] = _mtime(path)
        return watched


def _interesting(path: Path) -> bool:
    """是否监听该文件（规则/Skill/常见 md·json 配置）。"""
    return path.name in {"CLAUDE.md", "CLAUDE.local.md", "SKILL.md"} or path.suffix in {".md", ".json"}


def _mtime(path: Path) -> float | None:
    """读取单路径 mtime；不存在或不可读时返回 None。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None
