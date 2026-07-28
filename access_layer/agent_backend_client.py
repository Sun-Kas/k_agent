from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class AgentBackendClient:
    """Streaming client for the separate, stateless Agent Backend service."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def stream(self, payload: dict[str, Any], request_id: str) -> AsyncIterator[dict[str, Any]]:
        timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/internal/agent/run",
                headers={
                    "Accept": "application/x-ndjson",
                    "Content-Type": "application/json",
                    "X-Request-Id": request_id,
                },
                json=payload,
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Agent Backend request failed ({response.status_code}): {detail}"
                    )
                async for line in response.aiter_lines():
                    if line:
                        yield json.loads(line)

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self._base_url}/internal/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False
