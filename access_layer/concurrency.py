"""Access Layer 进程内并发守卫：全局请求槽位 + 按会话串行化。

在请求链路中的角色：`gateway.run` 在落盘用户消息前通过 `protect(session_id)`
占用一个全局槽与该会话锁，防止同会话双写历史，并限制同时进行的 agent run 数。

服务边界 / 部署约束：
- 锁表是进程本地的；当前假设单 worker。若 SERVER_WORKERS > 1，会话锁需换成
  Redis 等分布式实现，否则跨进程无法互斥。
- 与 SessionStore 的 asyncio.Lock 职责不同：本模块管「agent run 互斥」，
  Store 锁只管内存索引与落盘调用。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class RequestConcurrencyLimiter:
    """单 worker 部署下的进程内请求守卫。

    同时强制两类限制：
    1. 全局 BoundedSemaphore：与请求线程池容量对齐；
    2. 按 session_id 的 Lock：同一会话不能被两次 agent run 并发改写。
    """

    def __init__(self, max_concurrent_requests: int, acquire_timeout_seconds: float) -> None:
        """初始化全局信号量与会话锁表（均进程本地）。"""
        self.max_concurrent_requests = max(1, max_concurrent_requests)
        self.acquire_timeout_seconds = max(0.0, acquire_timeout_seconds)
        self._request_slots = asyncio.BoundedSemaphore(self.max_concurrent_requests)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def protect(self, session_id: str) -> AsyncIterator[None]:
        """为整次 agent run 预留一个全局槽位并独占目标会话。

        先拿全局槽再拿会话锁；任一步超时则转为 ConcurrencyLimitExceeded（对外 429）。
        """
        acquired_slot = False
        acquired_session_lock = False
        session_lock: asyncio.Lock | None = None
        try:
            await asyncio.wait_for(self._request_slots.acquire(), timeout=self.acquire_timeout_seconds)
            acquired_slot = True

            # 不同会话可并行；同会话必须串行，否则历史会出现 last-write-wins。
            session_lock = await self._get_session_lock(session_id)
            await asyncio.wait_for(session_lock.acquire(), timeout=self.acquire_timeout_seconds)
            acquired_session_lock = True
            yield
        except TimeoutError as exc:
            raise ConcurrencyLimitExceeded("Too many active requests, please retry later.") from exc
        finally:
            if acquired_session_lock and session_lock is not None:
                session_lock.release()
            if acquired_slot:
                self._request_slots.release()

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """惰性创建并缓存 per-session Lock；表本身用独立锁保护。"""
        async with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock


class ConcurrencyLimitExceeded(RuntimeError):
    """在超时内未能获得请求槽或会话锁时抛出，由网关映射为 HTTP 429。"""
