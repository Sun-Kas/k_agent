"""魔搭 MCP 广场只读客户端。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from access_layer.logging_config import log_event


class RegistryError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class McpRegistryClient:
    def __init__(self, *, base_url: str, timeout_seconds: float, api_token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.api_token = api_token.strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def list_servers(
        self, *, search: str = "", page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        body = {
            "filter": {},
            "page_number": max(1, page),
            "page_size": max(1, min(page_size, 100)),
            "search": search.strip(),
        }
        payload = await self._request("PUT", "/openapi/v1/mcp/servers", json_body=body)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = data.get("mcp_server_list") if isinstance(data.get("mcp_server_list"), list) else []
        return {
            "items": [item for item in items if isinstance(item, dict)],
            "total": int(data.get("total_count") or 0),
            "page": max(1, page),
            "pageSize": max(1, min(page_size, 100)),
        }

    async def get_latest(self, source_id: str) -> dict[str, Any]:
        encoded = quote(source_id, safe="")
        payload = await self._request("GET", f"/openapi/v1/mcp/servers/{encoded}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict) or not (data.get("id") or data.get("name")):
            raise RegistryError("MCP 条目不存在", status_code=404)
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json_body
                )
        except httpx.HTTPError as exc:
            log_event(
                "marketplace_fetch_failed",
                source="modelscope",
                errorCode="network",
                httpStatus=None,
            )
            raise RegistryError(str(exc)) from exc
        log_event(
            "marketplace_fetch_ok" if response.is_success else "marketplace_fetch_failed",
            source="modelscope",
            httpStatus=response.status_code,
        )
        if not response.is_success:
            raise RegistryError(
                f"魔搭 MCP 广场 HTTP {response.status_code}",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RegistryError("魔搭 MCP 广场返回了非对象 JSON")
        if payload.get("success") is False:
            raise RegistryError(str(payload.get("message") or "魔搭 MCP 广场请求失败"))
        return payload
