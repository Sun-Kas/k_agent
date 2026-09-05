"""SkillHub Open API 客户端：信封拆包、Header、302 下载。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from access_layer.home import agent_home
from access_layer.logging_config import log_event
from access_layer.skills.archive import MAX_SKILL_ZIP_BYTES


ALLOWED_DOWNLOAD_HOST_SUFFIXES = (
    "skillhub.cn",
    "aliyuncs.com",
    "myqcloud.com",
    "qcloud.com",
    "amazonaws.com",
    "github.com",
    "githubusercontent.com",
    "cdn.jsdelivr.net",
)


class SkillHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str = "skillhub") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class SkillHubClient:
    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.client_user_id = _client_user_id()

    def headers(self) -> dict[str, str]:
        headers = {"X-Client-User-Id": self.client_user_id}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def list_skills(
        self,
        *,
        keyword: str = "",
        category: str = "",
        sort_by: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if keyword:
            params["keyword"] = keyword
        if category:
            params["category"] = category
        if sort_by:
            params["sortBy"] = sort_by
        return await self._get_json("/api/skills", params=params, envelope=True)

    async def top(self) -> dict[str, Any]:
        return await self._get_json("/api/skills/top", envelope=True)

    async def categories(self) -> Any:
        return await self._get_json("/api/v1/categories", envelope=False)

    async def skill_detail(self, slug: str) -> dict[str, Any]:
        payload = await self._get_json(f"/api/v1/skills/{slug}", envelope=False)
        if not isinstance(payload, dict):
            raise SkillHubError("Skill 详情格式不正确")
        return normalize_skill_detail(payload)

    async def skill_markdown(self, slug: str) -> str:
        url = f"{self.base_url}/api/v1/skills/{slug}/file"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    max_redirects=5,
                ) as client:
                    response = await client.get(
                        url, params={"path": "SKILL.md"}, headers=self.headers()
                    )
                self._raise_for_auth(response)
                if response.status_code in {429, 500, 502, 503} and attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                if not response.is_success:
                    raise SkillHubError(
                        f"无法读取 SKILL.md（HTTP {response.status_code}）",
                        status_code=response.status_code,
                    )
                _assert_download_host(str(response.url))
                text = response.text
                if len(text.encode("utf-8")) > 1_000_000:
                    raise SkillHubError("SKILL.md 超过预览上限", status_code=413, code="too_large")
                return text
            except SkillHubError:
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                raise SkillHubError(str(exc), code="network") from exc
        raise SkillHubError(str(last_error or "skill file failed"), code="network")

    async def evaluation(self, slug: str) -> Any:
        return await self._get_json(f"/api/v1/skills/{slug}/evaluation", envelope=False)

    async def versions(self, slug: str) -> Any:
        return await self._get_json(f"/api/v1/skills/{slug}/versions", envelope=False)

    async def download_zip(self, slug: str) -> bytes:
        url = f"{self.base_url}/api/v1/download"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    max_redirects=5,
                ) as client:
                    response = await client.get(
                        url, params={"slug": slug}, headers=self.headers()
                    )
                self._raise_for_auth(response)
                if response.status_code in {429, 500, 502, 503} and attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                if not response.is_success:
                    raise SkillHubError(
                        f"Skill 下载失败（HTTP {response.status_code}）",
                        status_code=response.status_code,
                    )
                _assert_download_host(str(response.url))
                data = response.content
                if len(data) > MAX_SKILL_ZIP_BYTES:
                    raise SkillHubError("压缩包超过 20MB 限制", status_code=413, code="too_large")
                return data
            except SkillHubError:
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                raise SkillHubError(str(exc), code="network") from exc
        raise SkillHubError(str(last_error or "download failed"), code="network")

    async def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None, envelope: bool
    ) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.get(url, params=params, headers=self.headers())
                self._raise_for_auth(response)
                if response.status_code in {429, 500, 502, 503} and attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                log_event(
                    "marketplace_fetch_ok" if response.is_success else "marketplace_fetch_failed",
                    source="skillhub",
                    httpStatus=response.status_code,
                )
                if not response.is_success:
                    raise SkillHubError(
                        f"SkillHub HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                payload = response.json()
                return _unwrap(payload, envelope=envelope)
            except SkillHubError:
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                log_event("marketplace_fetch_failed", source="skillhub", errorCode="network")
                raise SkillHubError(str(exc), code="network") from exc
        raise SkillHubError(str(last_error or "request failed"), code="network")

    def _raise_for_auth(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            if not self.api_key:
                raise SkillHubError("未配置 SkillHub Key", status_code=response.status_code, code="missing_key")
            raise SkillHubError("SkillHub 鉴权失败", status_code=response.status_code, code="unauthorized")


def normalize_skill_detail(payload: dict[str, Any]) -> dict[str, Any]:
    """SkillHub 详情是 {skill, latestVersion, owner}；列表项是扁平对象。统一成 _skill_item 能读的字段。"""
    skill = payload.get("skill") if isinstance(payload.get("skill"), dict) else None
    if not skill:
        return payload
    latest = payload.get("latestVersion") if isinstance(payload.get("latestVersion"), dict) else {}
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
    tags = skill.get("tags")
    if isinstance(tags, dict):
        tag_list = [str(key) for key in tags]
    elif isinstance(tags, list):
        tag_list = [str(item) for item in tags]
    else:
        tag_list = []
    return {
        "slug": skill.get("slug") or payload.get("slug"),
        "name": skill.get("displayName") or skill.get("name"),
        "description": skill.get("summary_zh") or skill.get("summary") or skill.get("description_zh") or skill.get("description"),
        "version": latest.get("version") or skill.get("version"),
        "homepage": skill.get("homepage") or payload.get("homepage"),
        "category": skill.get("category"),
        "tags": tag_list,
        "downloads": stats.get("downloads"),
        "installs": stats.get("installs"),
        "stars": stats.get("stars"),
        "score": stats.get("score") or skill.get("score"),
        "ownerName": owner.get("displayName") or owner.get("handle") or skill.get("ownerName"),
        "changelog": latest.get("changelog"),
        "iconUrl": skill.get("iconUrl"),
        "labels": skill.get("labels"),
    }


def _unwrap(payload: Any, *, envelope: bool) -> Any:
    if not envelope:
        return payload
    if isinstance(payload, dict) and "data" in payload:
        code = payload.get("code")
        if code not in (0, "0", None, "success"):
            raise SkillHubError(str(payload.get("message") or "SkillHub 业务错误"))
        return payload["data"]
    return payload


def _client_user_id() -> str:
    configured = (os.getenv("K_AGENT_MARKETPLACE_CLIENT_ID") or "").strip()
    seed = configured or str(agent_home())
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _assert_download_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise SkillHubError("Skill 下载地址无效", code="bad_host")
    if any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_DOWNLOAD_HOST_SUFFIXES):
        return
    raise SkillHubError("Skill 下载跳转到了未允许的主机", code="bad_host")
