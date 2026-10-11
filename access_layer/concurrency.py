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
        self._session_locks: dict[str, asyncio.Lock] = {}   #session会话锁 只有协程竞争的是“同一个 asyncio.Lock 实例”，这个锁才能起到互斥作用
        self._session_locks_guard = asyncio.Lock()
        # Project 级串行锁：同一 Project 下的多个会话共享同一个 root_path，
        # 必须在会话锁之外再叠一层，防止两个归属同一 Project 的不同会话同时
        # 写同一批文件（例如都在改 CLAUDE.md）。按 session 维度的锁管不到这种
        # "不同 session_id、同一目录" 的并发。
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._project_locks_guard = asyncio.Lock()

    '''
    把一个异步生成器函数变成可以用 async with 的异步上下文管理器。

    最典型的例子：
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def connect():
        print("1. 建立连接")

        yield "connection"

        print("3. 关闭连接")

    async with connect() as conn:
    print("2. 使用连接", conn)



    1. 建立连接
        ↓
    yield
        ↓
    进入 async with
        ↓
    2. 使用连接
        ↓
    退出 async with
        ↓
    继续执行 yield 后面的代码
        ↓
    3. 关闭连接
    '''
    @asynccontextmanager
    async def protect(self, session_id: str, project_id: str | None = None) -> AsyncIterator[None]:
        """为整次 agent run 预留一个全局槽位并独占目标会话（及其 Project）。

        加锁顺序：全局槽 → 会话锁 → Project 锁（project_id 非空时）。任一步
        超时则转为 ConcurrencyLimitExceeded（对外 429）。释放严格逆序。
        """
        acquired_slot = False
        acquired_session_lock = False
        acquired_project_lock = False
        session_lock: asyncio.Lock | None = None
        project_lock: asyncio.Lock | None = None
        try:
            await asyncio.wait_for(self._request_slots.acquire(), timeout=self.acquire_timeout_seconds)
            acquired_slot = True

            # 不同会话可并行；同会话必须串行，否则历史会出现 last-write-wins。
            session_lock = await self._get_session_lock(session_id)
            await asyncio.wait_for(session_lock.acquire(), timeout=self.acquire_timeout_seconds)
            acquired_session_lock = True

            if project_id:
                # 同一 Project 下不同 session_id 仍可能并发跑，但它们共享同一个
                # root_path；没有这把锁，两个会话各自的工具写入会互相踩文件。
                project_lock = await self._get_project_lock(project_id)
                await asyncio.wait_for(project_lock.acquire(), timeout=self.acquire_timeout_seconds)
                acquired_project_lock = True
            yield
        except TimeoutError as exc:
            raise ConcurrencyLimitExceeded("Too many active requests, please retry later.") from exc
        finally:
            if acquired_project_lock and project_lock is not None:
                project_lock.release()
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

    async def _get_project_lock(self, project_id: str) -> asyncio.Lock:
        """惰性创建并缓存 per-project Lock；表本身用独立锁保护。"""
        async with self._project_locks_guard:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                self._project_locks[project_id] = lock
            return lock

    @asynccontextmanager
    async def protect_idle_session(self, session_id: str) -> AsyncIterator[None]:
        """控制面操作只接受空闲会话；不等待正在运行的 Agent。"""

        session_lock = await self._get_session_lock(session_id)
        if session_lock.locked():
            raise SessionAlreadyRunning("Session has an active run")
        await session_lock.acquire()
        try:
            yield
        finally:
            session_lock.release()


class ConcurrencyLimitExceeded(RuntimeError):
    """在超时内未能获得请求槽或会话锁时抛出，由网关映射为 HTTP 429。"""


class SessionAlreadyRunning(RuntimeError):
    """手动 compact 等控制操作不能与同会话 run 并发。"""
