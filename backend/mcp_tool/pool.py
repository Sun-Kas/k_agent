"""跨无状态 Agent run 复用 MCP 连接，避免每轮冷启动 uvx/npx。

Agent Backend 按设计无会话状态：Access Layer 每轮把自包含的 server 列表
随请求送来。若每轮 `connect` + run 结束 `close`，stdio 型 MCP（uvx/npx
拉起子进程、握手 initialize、list_tools）会变成延迟和 CPU 的主开销，
HTTP 型也会反复鉴权握手。池把「连接」从请求生命周期里拆出来：相同指纹
复用，配置变了才新建；对话内容仍不进池、不跨 run 共享。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from backend.mcp_tool.client import McpServerConfig, McpSession


DEFAULT_IDLE_TTL_SECONDS = 600.0


def fingerprint_config(config: McpServerConfig) -> str:
    """从一切影响连接本身的字段推导稳定池键。"""

    payload = json.dumps(
        {
            "id": config.id,
            "type": config.type,
            "command": config.command,
            "args": list(config.args or []),
            "env": dict(config.env or {}),
            "envPassthrough": sorted(config.env_passthrough or []),
            "cwd": config.cwd,
            "url": config.url,
            "bearerTokenEnv": config.bearer_token_env,
            "headers": dict(config.headers or {}),
            "envHeaders": dict(config.env_headers or {}),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _PooledSession:
    """一条可复用连接 + 决定其去留的租约/空闲记账。"""

    config: McpServerConfig
    session: McpSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leases: int = 0
    last_used: float = field(default_factory=time.monotonic)
    # 墓碑：现有租约继续用这条连接，新 acquire 当它不存在、另开一条。
    # 置位来源：传输不健康，或 close_all(force=False) 要换代但不能杀进行中的 run。
    retire: bool = False


class McpSessionPool:
    """本进程（uvicorn worker）内的 MCP 连接池，挂在 FastAPI `app.state`。

    进程级、内存级：不按对话/thread/run 隔离，也不跨 Access Layer 或其它
    worker 共享。键是连接设置指纹；相同配置的并发 run 共用一条会话，
    用 `leases` 记账。请求级 `McpClientManager` 只租用/归还，不自己长连。
    """

    def __init__(self, *, idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS) -> None:
        """idle TTL 控制无租约会话何时被驱逐。"""
        self._idle_ttl_seconds = idle_ttl_seconds
        self._entries: dict[str, _PooledSession] = {}
        # 必须用 asyncio.Lock：并发 run 是同一线程里的协程，在 await 处交错。
        # threading.RLock 按线程认人，同线程二次 acquire 会直接放行，等于没锁。
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        config: McpServerConfig,
        *,
        connect_timeout_seconds: float,
    ) -> tuple[str, McpSession]:
        """返回该配置下已连接的会话；冷启动时用 per-entry 锁防重复拉起子进程。"""

        fingerprint = fingerprint_config(config)
        # 池级锁只保护字典和租约记账，不包住 connect：慢握手不能堵住别的指纹。
        # 并发 run 会在 await 处交错；无锁会出现双建条目、leases 对不上、
        # 一边驱逐一边 acquire 把正在关的会话又租出去。
        async with self._lock:
            await self._evict_idle_locked()
            entry = self._entries.get(fingerprint)
            if entry is None or entry.retire:
                # 退役条目不能再租；换新对象进字典，旧连接只留给还握着它的 run。
                entry = _PooledSession(config=config, session=McpSession(config))
                self._entries[fingerprint] = entry
            entry.leases += 1
        try:
            # 同一指纹的冷启动互斥：第二个等待者进来时连接往往已经好了。
            async with entry.lock:
                # `McpSession.session` 才是 SDK ClientSession；None = 未连上或已死。
                if entry.session.session is None:
                    # 失败过的 wrapper 上可能残留 _runner/_ready，不能再 connect。
                    entry.session = McpSession(config)
                    async with asyncio.timeout(connect_timeout_seconds):
                        await entry.session.connect()
        except BaseException:
            await self.release(fingerprint, healthy=False)
            raise
        return fingerprint, entry.session

    async def release(self, fingerprint: str, *, healthy: bool = True) -> None:
        """归还租约；不健康则标记 retire，无租约时真正关闭。"""

        async with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return
            entry.leases = max(0, entry.leases - 1)
            entry.last_used = time.monotonic()
            if not healthy:
                entry.retire = True
            if entry.leases > 0 or not entry.retire:
                return
            self._entries.pop(fingerprint, None)
        await self._close_entry(entry)

    async def close_all(self, *, force: bool = False) -> None:
        """立刻关闭空闲会话；仍有租约的标记 retire，等 run 结束再关。

        `force=True` 仅适合进程关闭：强关仍在使用的会话会让进行中工具调用失败。
        """

        async with self._lock:
            closing: list[_PooledSession] = []
            for fingerprint, entry in list(self._entries.items()):
                # 有人还在用：只标退役，不关传输；最后一次 release() 才会 close。
                if entry.leases > 0 and not force:
                    entry.retire = True
                    continue
                self._entries.pop(fingerprint, None)
                closing.append(entry)
        # 锁外关连接，避免慢 close 堵住其它 acquire。
        for entry in closing:
            await self._close_entry(entry)

    async def stats(self) -> dict[str, int]:
        """健康/调试端点用的池占用统计。"""

        async with self._lock:
            return {
                "pooledSessions": len(self._entries),
                "leasedSessions": sum(
                    1 for entry in self._entries.values() if entry.leases > 0
                ),
            }

    async def _evict_idle_locked(self) -> None:
        """丢掉超过空闲窗口且无租约的会话。"""

        deadline = time.monotonic() - self._idle_ttl_seconds
        stale = [
            fingerprint
            for fingerprint, entry in self._entries.items()
            if entry.leases == 0 and entry.last_used < deadline
        ]
        for fingerprint in stale:
            entry = self._entries.pop(fingerprint)
            # Closing under the pool lock keeps eviction simple; a stuck server
            # is bounded by the session's own 3s close timeout.
            await self._close_entry(entry)

    @staticmethod
    async def _close_entry(entry: _PooledSession) -> None:
        """关闭一条池内会话；失败吞掉，避免泄漏到调用方。"""

        try:
            await entry.session.close()
        except Exception:
            pass
