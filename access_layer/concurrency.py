from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(frozen=True, slots=True)
class ConcurrencySnapshot:
    max_concurrent_requests: int
    active_requests: int
    available_request_slots: int
    session_lock_count: int


class RequestConcurrencyLimiter:
    """Process-local request guard for the single-worker deployment.

    Two limits are enforced here:
    1. a global slot limit, matching the request thread pool size;
    2. a per-session lock, so one conversation cannot be mutated by two agent
       runs at the same time.

    This lock table is intentionally process-local. If SERVER_WORKERS is raised
    above 1, replace the session lock path with a distributed lock such as Redis.
    """

    def __init__(self, max_concurrent_requests: int, acquire_timeout_seconds: float) -> None:
        self.max_concurrent_requests = max(1, max_concurrent_requests)
        self.acquire_timeout_seconds = max(0.0, acquire_timeout_seconds)
        self._request_slots = asyncio.BoundedSemaphore(self.max_concurrent_requests)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()
        self._active_requests = 0
        self._active_guard = asyncio.Lock()

    @asynccontextmanager
    async def protect(self, session_id: str) -> AsyncIterator[None]:
        """Reserve one request slot and the target session for a full agent run."""
        acquired_slot = False
        acquired_session_lock = False
        session_lock: asyncio.Lock | None = None
        try:
            await asyncio.wait_for(self._request_slots.acquire(), timeout=self.acquire_timeout_seconds)
            acquired_slot = True
            async with self._active_guard:
                self._active_requests += 1

            # Different sessions can run in parallel, but the same session must
            # serialize to prevent last-write-wins history corruption.
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
                async with self._active_guard:
                    self._active_requests -= 1
                self._request_slots.release()

    async def snapshot(self) -> ConcurrencySnapshot:
        async with self._active_guard:
            active = self._active_requests
        return ConcurrencySnapshot(
            max_concurrent_requests=self.max_concurrent_requests,
            active_requests=active,
            available_request_slots=max(0, self.max_concurrent_requests - active),
            session_lock_count=len(self._session_locks),
        )

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock


class ConcurrencyLimitExceeded(RuntimeError):
    pass
