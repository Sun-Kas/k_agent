"""跨无状态 Agent run 复用 MCP 连接，避免每轮冷启动 uvx/npx。

Access Layer 每轮下发自包含 server 列表；朴素实现会每轮起停 stdio 子进程。
池按连接设置指纹键控：相同设置复用，变更则新建；对话内容绝不跨 run 共享。
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
    retire: bool = False


class McpSessionPool:
    """把 MCP 会话租给请求级 manager，而不是每轮重连。"""

    def __init__(self, *, idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS) -> None:
        """idle TTL 控制无租约会话何时被驱逐。"""
        self._idle_ttl_seconds = idle_ttl_seconds
        self._entries: dict[str, _PooledSession] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        config: McpServerConfig,
        *,
        connect_timeout_seconds: float,
    ) -> tuple[str, McpSession]:
        """返回该配置下已连接的会话；冷启动时用 per-entry 锁防重复拉起子进程。"""

        fingerprint = fingerprint_config(config)
        async with self._lock:
            await self._evict_idle_locked()
            entry = self._entries.get(fingerprint)
            if entry is None or entry.retire:
                entry = _PooledSession(config=config, session=McpSession(config))
                self._entries[fingerprint] = entry
            entry.leases += 1
        try:
            # The per-entry lock keeps concurrent runs from spawning duplicate
            # child processes for the same server during a cold start.
            async with entry.lock:
                if entry.session.session is None:
                    # A previous connect failed or the child process exited, so the
                    # old session object cannot be reused for a second attempt.
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
                if entry.leases > 0 and not force:
                    entry.retire = True
                    continue
                self._entries.pop(fingerprint, None)
                closing.append(entry)
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
