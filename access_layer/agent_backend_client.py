"""Access Layer → Agent Backend 的私有 HTTP 客户端。

在请求链路中的角色：把公开网关 / Team Runtime 组装好的载荷 POST 到
`/internal/agent/run`，以 NDJSON 流式回传 AG-UI 事件；配置/健康类接口
走短超时的 JSON GET/POST。

服务边界：仅访问 Agent Backend 的 `/internal/*`，不直接触达模型或工具；
流式读超时关闭（read=None），避免长 agent run 被客户端提前掐断。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class AgentBackendClient:
    """面向独立、无状态 Agent Backend 的流式 HTTP 客户端。"""

    def __init__(self, base_url: str) -> None:
        """记录后端基址；每次调用新建 AsyncClient，避免跨请求共享连接状态。"""
        self._base_url = base_url.rstrip("/")

    async def stream(self, payload: dict[str, Any], request_id: str) -> AsyncIterator[dict[str, Any]]:
        """流式读取 `/internal/agent/run` 的 NDJSON 事件，不缓冲整次运行结果。

        `X-Request-Id` 透传以便两端日志关联；read 超时关闭以支持长时间工具调用。
        """
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
        """探测私有 `/internal/health`；网络/非 200 均视为不可用。"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self._base_url}/internal/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_json(self, path: str) -> dict[str, Any]:
        """GET 私有控制面 JSON（如 runtime status / MCP capabilities）。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self._base_url}{path}")
        response.raise_for_status()
        return response.json()

    async def post_json(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST 私有控制面操作（如 MCP reload、approval、prompt reset）。

        超时刻意高于后端单 server 启动时间：冷启动 STDIO MCP 可能含 uvx/npx
        下载；配置已在接入层落盘后，不应因本客户端过早取消而留下不一致状态。
        """
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{self._base_url}{path}", json=payload or {}
            )
        response.raise_for_status()
        return response.json()
