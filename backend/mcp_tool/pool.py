"""Process-wide reuse of MCP connections across stateless agent runs.

Each run receives a self-contained server list from the Access Layer, so a
naive implementation spawns and tears down a stdio child process (often an
uvx/npx cold start) on every turn. The pool keys live sessions by their
connection settings: identical settings reuse one connection, changed settings
produce a new one, and nothing conversational is shared between runs.
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
    """Derive a stable key from everything that affects the connection itself."""

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
    """One reusable connection plus the bookkeeping that decides its fate."""

    config: McpServerConfig
    session: McpSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leases: int = 0
    last_used: float = field(default_factory=time.monotonic)
    retire: bool = False


class McpSessionPool:
    """Lease MCP sessions to request-scoped managers instead of reconnecting."""

    def __init__(self, *, idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS) -> None:
        """初始化对象依赖和内部状态。"""
        self._idle_ttl_seconds = idle_ttl_seconds
        self._entries: dict[str, _PooledSession] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        config: McpServerConfig,
        *,
        connect_timeout_seconds: float,
    ) -> tuple[str, McpSession]:
        """Return a connected session for this configuration, reusing when possible."""

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
        """Return a leased session to the pool, closing it when unusable."""

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
        """Close idle sessions now and retire leased ones as their runs finish.

        Forcing is only correct at shutdown: closing a session that an in-flight
        run still holds would fail that run's remaining tool calls.
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
        """Expose pool occupancy for health and debugging endpoints."""

        async with self._lock:
            return {
                "pooledSessions": len(self._entries),
                "leasedSessions": sum(
                    1 for entry in self._entries.values() if entry.leases > 0
                ),
            }

    async def _evict_idle_locked(self) -> None:
        """Drop unleased sessions that outlived the idle window."""

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
        """Close one pooled session without letting failures leak to callers."""

        try:
            await entry.session.close()
        except Exception:
            pass
