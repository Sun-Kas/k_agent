"""进程内 TTL 缓存，同一 key 合并 in-flight 请求。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time
from typing import TypeVar


T = TypeVar("T")


class TtlCache:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, object]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(
        self,
        key: str,
        ttl_seconds: float,
        fetcher: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """返回 (value, from_cache)。fetch 失败时回退未过期失败前的成功值。"""

        now = time.monotonic()
        cached = self._values.get(key)
        if cached and cached[0] > now:
            return cached[1], True  # type: ignore[return-value]

        async with self._lock:
            cached = self._values.get(key)
            if cached and cached[0] > now:
                return cached[1], True  # type: ignore[return-value]
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(fetcher())
                self._inflight[key] = task

        try:
            value = await task
            self._values[key] = (time.monotonic() + ttl_seconds, value)
            return value, False
        except Exception:
            stale = self._values.get(key)
            if stale is not None:
                return stale[1], True  # type: ignore[return-value]
            raise
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
