"""HTTP client for streaming requests from the access layer to the Agent Backend."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class AgentBackendClient:
    """Streaming client for the separate, stateless Agent Backend service."""

    def __init__(self, base_url: str) -> None:
        """初始化对象依赖和内部状态。"""
        self._base_url = base_url.rstrip("/")

    async def stream(self, payload: dict[str, Any], request_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded backend events without buffering the complete agent run."""

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
        """Return whether the private Agent Backend answers its health endpoint."""

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self._base_url}/internal/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_json(self, path: str) -> dict[str, Any]:
        """说明 get_json 在当前模块中的具体职责。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self._base_url}{path}")
        response.raise_for_status()
        return response.json()

    async def post_json(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST private control-plane operations such as MCP reloads."""

        # A cold STDIO MCP start can include an uvx/npx package download. Keep
        # this above the backend's per-server timeout so reload is not cancelled
        # at the access-layer boundary after the configuration was already saved.
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{self._base_url}{path}", json=payload or {}
            )
        response.raise_for_status()
        return response.json()

    async def put_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """说明 put_json 在当前模块中的具体职责。"""
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.put(f"{self._base_url}{path}", json=payload)
        response.raise_for_status()
        return response.json()
