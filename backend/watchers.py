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
    """对 prompt 相关文件做轻量 **mtime 轮询**，不用内核文件事件（inotify / FSEvents）。

    原生 FS hook 要额外库（如 watchdog）、在 Docker/NFS/部分 macOS 上会丢事件。
    这里只 `stat` 少量路径，不引那些依赖。行为保守：任意一处 mtime 变化就整表
    失效 prompt/memory 缓存，不做「只清某一个文件」的精细失效。
    """

    def __init__(self, roots: list[Path], on_change: Callable[[str], None], interval_seconds: float = 1.5):
        """绑定监听根路径、变更回调与轮询间隔，并准备空快照状态。"""
        self.roots = roots
        self.on_change = on_change
        self.interval_seconds = interval_seconds
        self.state = WatchedPathState()
        self._task: asyncio.Task[None] | None = None
        # 协程间的「红绿灯」：默认灭（未停止）。set() 点亮后，所有 wait() 立刻醒来，
        # 且一直亮着；is_set() 只是看灯，不阻塞。
        self._stop = asyncio.Event()

    def start(self) -> None:
        """启动后台轮询任务（幂等：已启动则忽略）。"""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """发出停止信号并等待轮询任务退出。"""
        # 点亮开关：正在 wait() 的 _run 立刻醒来；is_set() 之后为真，循环结束。
        self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None

    async def _run(self) -> None:
        """周期性对比 mtime 快照；有差异则调用一次 on_change。"""
        # 先取基线快照，避免把启动时的既有状态误判成一次变更。
        self.state.mtimes = self._snapshot()
        while not self._stop.is_set():
            # is_set()：只看灯，不阻塞。wait()：躺下等到 set()，或下面 timeout 到期。
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                # stop() 已 set：正常返回，不进 except，下一圈 while 条件为假就退出。
            except TimeoutError:
                # 间隔到了、还没停：对比快照。reason 固定字符串，生命周期只当「有文件变了」。
                current = self._snapshot()
                # 整本 path→mtime 比较：新增、删除、内容改写都会让字典不等。
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
        # inode 元数据：内容最后修改时间（秒，浮点）。内容没变则两次 snapshot 相等。
        return path.stat().st_mtime
    except OSError:
        return None
