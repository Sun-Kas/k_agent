"""广场聚合：归一化、对照 catalog、安装落盘。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from typing import Any

from fastapi import HTTPException

from access_layer.catalog import RuntimeCatalog, catalog_fields_from_frontmatter, read_mcp_config, skill_catalog_row
from access_layer.home import mcp_config_path as default_mcp_config_path
from access_layer.logging_config import log_event
from access_layer.marketplace.cache import TtlCache
from access_layer.marketplace.install_mcp import map_install, suggested_local_id
from access_layer.marketplace.install_skill import download_and_install
from access_layer.marketplace.match import match_installed
from access_layer.marketplace.mcp_registry import McpRegistryClient, RegistryError
from access_layer.marketplace.models import listing, marketplace_item
from access_layer.marketplace.skillhub import SkillHubClient, SkillHubError, normalize_skill_detail
from access_layer.skills.archive import normalize_imported_skill_id
from access_layer.skills.frontmatter import parse_bool, parse_markdown_frontmatter
from access_layer.storage import write_json_atomic


LIST_TTL = 60.0
DETAIL_TTL = 300.0
MCP_SOURCE = "modelscope"


class MarketplaceService:
    def __init__(
        self,
        catalog: RuntimeCatalog,
        *,
        mcp_config_path: Path | None = None,
        mcp_registry: McpRegistryClient | None = None,
        skillhub: SkillHubClient | None = None,
        cache: TtlCache | None = None,
    ) -> None:
        self.catalog = catalog
        self.mcp_config_path = mcp_config_path or default_mcp_config_path()
        self.mcp_registry = mcp_registry or McpRegistryClient(
            base_url=os.getenv("MODELSCOPE_MCP_BASE_URL") or "https://www.modelscope.cn",
            timeout_seconds=float(os.getenv("MODELSCOPE_MCP_TIMEOUT_SECONDS") or 15),
            api_token=os.getenv("MODELSCOPE_API_TOKEN") or "",
        )
        self.skillhub = skillhub or SkillHubClient(
            base_url=os.getenv("SKILLHUB_BASE_URL") or "https://api.skillhub.cn",
            api_key=os.getenv("SKILLHUB_API_KEY") or "",
            timeout_seconds=float(os.getenv("SKILLHUB_TIMEOUT_SECONDS") or 20),
        )
        self.cache = cache or TtlCache()

    async def list_mcp(self, *, q: str = "", page: int = 1, page_size: int = 24) -> dict[str, Any]:
        key = f"mcp:list:{q}:{page}:{page_size}"
        try:
            payload, from_cache = await self.cache.get_or_fetch(
                key, LIST_TTL, lambda: self.mcp_registry.list_servers(search=q, page=page, page_size=page_size)
            )
        except RegistryError as exc:
            return listing([], source_status="unavailable", warnings=[str(exc)], page=page, page_size=page_size)
        items = [self._mcp_item(entry) for entry in payload.get("items") or []]
        status = "degraded" if from_cache and not items else ("degraded" if from_cache else "ok")
        return listing(
            items,
            source_status=status if items or status == "ok" else "degraded",
            warnings=[],
            page=int(payload.get("page") or page),
            page_size=page_size,
            total=int(payload.get("total") or 0),
        )

    async def mcp_item(self, source_id: str) -> dict[str, Any]:
        server = await self._mcp_server(source_id)
        return self._mcp_item(server, include_preview=True)

    async def preview_mcp(self, source_id: str) -> dict[str, Any]:
        item = await self.mcp_item(source_id)
        local_id = item.get("localId") or suggested_local_id(source_id)
        exists = any(str(row.get("id")) == local_id for row in self.catalog.mcp_summaries())
        exists = exists or any(str(row.get("id")) == local_id for row in self._mcp_runtime_servers())
        item["conflict"] = {"localId": local_id, "exists": exists}
        return item

    async def install_mcp(
        self,
        source_id: str,
        *,
        local_id: str | None = None,
        enabled: bool = True,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        server = await self._mcp_server(source_id)
        mapped = map_install(server)
        target_id = suggested_local_id(source_id, local_id)
        log_event("marketplace_install_started", source=MCP_SOURCE, sourceId=source_id, localId=target_id)
        if mapped.get("blockedReason"):
            raise HTTPException(status_code=400, detail=f"无法安装该 MCP：{mapped['blockedReason']}")
        allowed_env = set(mapped.get("envKeys") or [])
        allowed_headers = set(mapped.get("headerKeys") or [])
        env_values = {key: value for key, value in (env or {}).items() if key in allowed_env}
        header_values = {key: value for key, value in (headers or {}).items() if key in allowed_headers}
        defaults = (mapped.get("draft") or {}).get("env") or {}
        merged_env = {**defaults, **env_values}
        missing = [
            key for key in (mapped.get("missingEnvKeys") or [])
            if not str(merged_env.get(key) or header_values.get(key) or "").strip()
        ]
        if missing:
            raise HTTPException(status_code=400, detail="缺少必填配置：" + ", ".join(missing))
        if any(str(row.get("id")) == target_id for row in self._mcp_runtime_servers()) or any(
            str(row.get("id")) == target_id for row in self.catalog.mcp_summaries()
        ):
            raise HTTPException(status_code=409, detail=f'MCP "{target_id}" 已存在')
        servers = self._mcp_runtime_servers()
        draft = dict(mapped["draft"])
        servers.append(
            {
                "id": target_id,
                "name": mapped.get("title") or target_id,
                "description": mapped.get("description") or "",
                "type": draft.get("type", "stdio"),
                "command": draft.get("command"),
                "args": draft.get("args") or [],
                "env": merged_env,
                "headers": header_values,
                "enabled": enabled,
                "url": draft.get("url"),
            }
        )
        _write_mcp_servers(self.mcp_config_path, servers)
        summaries = self.catalog.mcp_summaries()
        summaries.append(
            {
                "id": target_id,
                "name": mapped.get("title") or target_id,
                "description": mapped.get("description") or "",
                "enabled": enabled,
                "marketplace": _marketplace_meta(MCP_SOURCE, source_id, server.get("version")),
            }
        )
        self.catalog.write_mcp_summaries(summaries)
        log_event("marketplace_install_succeeded", source=MCP_SOURCE, sourceId=source_id, localId=target_id)
        return {"ok": True, "localId": target_id, "item": self._mcp_item(server, include_preview=True)}

    async def uninstall_mcp(self, local_id: str) -> dict[str, Any]:
        current = self._mcp_runtime_servers()
        servers = [item for item in current if str(item.get("id")) != local_id]
        if len(servers) == len(current) and not any(
            str(item.get("id")) == local_id for item in self.catalog.mcp_summaries()
        ):
            raise HTTPException(status_code=404, detail="MCP 未安装")
        _write_mcp_servers(self.mcp_config_path, servers)
        self.catalog.write_mcp_summaries(
            [item for item in self.catalog.mcp_summaries() if str(item.get("id")) != local_id]
        )
        return {"ok": True, "localId": local_id}

    async def list_skills(
        self,
        *,
        q: str = "",
        category: str = "",
        sort_by: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        key = f"skills:list:{q}:{category}:{sort_by}:{page}:{page_size}"
        try:
            payload, from_cache = await self.cache.get_or_fetch(
                key,
                LIST_TTL,
                lambda: self.skillhub.list_skills(
                    keyword=q, category=category, sort_by=sort_by, page=page, page_size=page_size
                ),
            )
        except SkillHubError as exc:
            return listing([], source_status="unavailable", warnings=[str(exc)], page=page, page_size=page_size)
        skills = _as_list(payload, "skills")
        items = [self._skill_item(item) for item in skills]
        return listing(
            items,
            source_status="degraded" if from_cache else "ok",
            warnings=[],
            page=int(payload.get("page") or page) if isinstance(payload, dict) else page,
            page_size=page_size,
            total=int(payload.get("total") or len(items)) if isinstance(payload, dict) else len(items),
        )

    async def top_skills(self) -> dict[str, Any]:
        try:
            payload, from_cache = await self.cache.get_or_fetch(
                "skills:top", LIST_TTL, self.skillhub.top
            )
        except SkillHubError as exc:
            return listing([], source_status="unavailable", warnings=[str(exc)])
        skills = _as_list(payload, "skills") or _as_list(payload, "items")
        if isinstance(payload, list):
            skills = payload
        return listing(
            [self._skill_item(item) for item in skills if isinstance(item, dict)],
            source_status="degraded" if from_cache else "ok",
            warnings=[],
        )

    async def categories(self) -> dict[str, Any]:
        try:
            payload, from_cache = await self.cache.get_or_fetch(
                "skills:categories", LIST_TTL, self.skillhub.categories
            )
        except SkillHubError as exc:
            return {"items": [], "sourceStatus": "unavailable", "warnings": [str(exc)]}
        items = payload if isinstance(payload, list) else _as_list(payload, "categories") or _as_list(payload, "items")
        return {
            "items": items,
            "sourceStatus": "degraded" if from_cache else "ok",
            "warnings": [],
        }

    async def skill_item(self, slug: str, *, include_preview: bool = True) -> dict[str, Any]:
        key = f"skills:detail:{slug}"
        payload, _ = await self.cache.get_or_fetch(key, DETAIL_TTL, lambda: self.skillhub.skill_detail(slug))
        return self._skill_item(payload, include_preview=include_preview)

    async def skill_evaluation(self, slug: str) -> dict[str, Any]:
        payload, _ = await self.cache.get_or_fetch(
            f"skills:eval:{slug}", DETAIL_TTL, lambda: self.skillhub.evaluation(slug)
        )
        return {"slug": slug, "evaluation": _public_evaluation(payload)}

    async def skill_versions(self, slug: str) -> dict[str, Any]:
        payload, _ = await self.cache.get_or_fetch(
            f"skills:versions:{slug}", DETAIL_TTL, lambda: self.skillhub.versions(slug)
        )
        versions = payload if isinstance(payload, list) else _as_list(payload, "versions")
        public = []
        for item in versions:
            if not isinstance(item, dict):
                continue
            public.append(
                {
                    "version": item.get("version") or item.get("semver"),
                    "createdAt": item.get("createdAt") or item.get("publishedAt"),
                }
            )
        return {"slug": slug, "versions": public}

    async def preview_skill(self, slug: str) -> dict[str, Any]:
        try:
            item = await self.skill_item(slug)
        except SkillHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        local_id = item.get("localId") or normalize_imported_skill_id(slug)
        exists = (self.catalog.skill_dir / local_id).exists() or any(
            str(row.get("id")) == local_id for row in self.catalog.skill_summaries()
        )
        item["conflict"] = {"localId": local_id, "exists": exists}
        try:
            markdown, _ = await self.cache.get_or_fetch(
                f"skills:file:{slug}:SKILL.md",
                DETAIL_TTL,
                lambda: self.skillhub.skill_markdown(slug),
            )
            item["body"] = markdown
        except SkillHubError:
            item["body"] = None
        return item

    async def install_skill(
        self, slug: str, *, local_id: str | None = None, enabled: bool = True
    ) -> dict[str, Any]:
        log_event("marketplace_install_started", source="skillhub", sourceId=slug, localId=local_id)
        try:
            detail = await self.skillhub.skill_detail(slug)
            skill_id, skill_name, skill_dir = await download_and_install(
                self.skillhub,
                slug,
                skills_root=self.catalog.skill_dir,
                skill_id_override=local_id,
            )
        except HTTPException:
            log_event("marketplace_install_failed", source="skillhub", sourceId=slug, errorCode="http")
            raise
        except SkillHubError as exc:
            log_event("marketplace_install_failed", source="skillhub", sourceId=slug, errorCode=exc.code)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        frontmatter, _ = parse_markdown_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        )
        summaries = self.catalog.skill_summaries()
        if any(str(item.get("id")) == skill_id for item in summaries):
            shutil.rmtree(skill_dir, ignore_errors=True)
            raise HTTPException(status_code=409, detail=f'Skill "{skill_id}" 已存在')
        summaries.append(
            skill_catalog_row(
                {
                    "id": skill_id,
                    "name": skill_name,
                    "description": str(frontmatter.get("description") or ""),
                    "enabled": enabled and not parse_bool(frontmatter.get("disable-model-invocation"), False),
                    **catalog_fields_from_frontmatter(frontmatter),
                    "marketplace": _marketplace_meta(
                        "skillhub", slug, detail.get("version") or frontmatter.get("version")
                    ),
                }
            )
        )
        self.catalog.write_skill_summaries(summaries)
        log_event("marketplace_install_succeeded", source="skillhub", sourceId=slug, localId=skill_id)
        return {"ok": True, "localId": skill_id, "name": skill_name, "item": self._skill_item(detail)}

    async def uninstall_skill(self, local_id: str) -> dict[str, Any]:
        skill_dir = self.catalog.skill_dir / local_id
        summaries = self.catalog.skill_summaries()
        if not skill_dir.exists() and not any(str(item.get("id")) == local_id for item in summaries):
            raise HTTPException(status_code=404, detail="Skill 未安装")
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        self.catalog.write_skill_summaries(
            [item for item in summaries if str(item.get("id")) != local_id]
        )
        return {"ok": True, "localId": local_id}

    async def _mcp_server(self, source_id: str) -> dict[str, Any]:
        try:
            payload, _ = await self.cache.get_or_fetch(
                f"mcp:detail:{source_id}",
                DETAIL_TTL,
                lambda: self.mcp_registry.get_latest(source_id),
            )
        except RegistryError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        server = payload.get("server") if isinstance(payload.get("server"), dict) else payload
        if not isinstance(server, dict) or not (server.get("id") or server.get("name")):
            raise HTTPException(status_code=404, detail="MCP 条目不存在")
        return server

    def _mcp_item(self, server: dict[str, Any], *, include_preview: bool = False) -> dict[str, Any]:
        source_id = str(server.get("id") or server.get("name") or "")
        locales = server.get("locales") if isinstance(server.get("locales"), dict) else {}
        zh = locales.get("zh") if isinstance(locales.get("zh"), dict) else {}
        title = str(zh.get("name") or server.get("chinese_name") or server.get("title") or server.get("name") or source_id)
        summary = str(zh.get("description") or server.get("description") or "")
        installed = match_installed(self.catalog.mcp_summaries(), source=MCP_SOURCE, source_id=source_id)
        preview = None
        mapped = None
        if include_preview:
            mapped = map_install(server)
            preview = {
                "transport": mapped.get("transport"),
                "command": mapped.get("command"),
                "args": mapped.get("args") or [],
                "url": mapped.get("url"),
                "envKeys": mapped.get("envKeys") or [],
                "headerKeys": mapped.get("headerKeys") or [],
                "secretKeys": mapped.get("secretKeys") or [],
                "fieldMeta": mapped.get("fieldMeta") or [],
                "missingEnvKeys": mapped.get("missingEnvKeys") or [],
                "blockedReason": mapped.get("blockedReason"),
            }
        repo = server.get("repository") if isinstance(server.get("repository"), dict) else {}
        homepage = (
            str(server.get("source_url") or "")
            or str(repo.get("url") or "")
            or (f"https://www.modelscope.cn/mcp/servers/{source_id}" if source_id.startswith("@") else "")
        )
        extra: dict[str, Any] = {
            "ownerName": str(server.get("author") or server.get("publisher") or "") or None,
        }
        if include_preview and server.get("readme"):
            extra["body"] = str(server.get("readme"))
        return marketplace_item(
            kind="mcp",
            source=MCP_SOURCE,
            source_id=source_id,
            title=title,
            summary=summary,
            version=str(server.get("version") or "") or None,
            icon_url=_icon_url(server.get("logo_url"), server.get("icons")),
            icons=server.get("icons") if isinstance(server.get("icons"), list) else [],
            homepage=homepage or None,
            categories=list(server.get("categories") or []),
            tags=list(server.get("tags") or []),
            official_status="verified" if server.get("is_verified") else _official_status(server),
            installed=installed is not None,
            local_id=str(installed["id"]) if installed else suggested_local_id(source_id),
            install_preview=preview,
            extra=extra,
        )

    def _skill_item(self, raw: dict[str, Any], *, include_preview: bool = False) -> dict[str, Any]:
        raw = normalize_skill_detail(raw)
        slug = str(raw.get("slug") or raw.get("id") or "")
        installed = match_installed(self.catalog.skill_summaries(), source="skillhub", source_id=slug)
        preview = None
        if include_preview:
            preview = {
                "archive": "zip",
                "requiresFrontmatter": ["name", "description"],
                "blockedReason": None,
            }
        stats = {
            "downloads": raw.get("downloads") or raw.get("downloadCount"),
            "installs": raw.get("installs") or raw.get("installCount"),
            "score": raw.get("score") or raw.get("qualityScore"),
        }
        return marketplace_item(
            kind="skill",
            source="skillhub",
            source_id=slug,
            title=str(raw.get("name") or raw.get("title") or slug),
            summary=str(raw.get("description") or raw.get("summary") or ""),
            version=str(raw.get("version") or "") or None,
            icon_url=_icon_url(raw.get("iconUrl"), raw.get("icon"), raw.get("icons")),
            homepage=str(raw.get("homepage") or raw.get("url") or "") or None,
            categories=[str(raw["category"])] if raw.get("category") else list(raw.get("categories") or []),
            tags=list(raw.get("tags") or []),
            stats=stats,
            installed=installed is not None,
            local_id=str(installed["id"]) if installed else normalize_imported_skill_id(slug),
            install_preview=preview,
            extra={
                "ownerName": str(raw.get("ownerName") or "") or None,
                "changelog": str(raw.get("changelog") or "") or None,
            },
        )

    def _mcp_runtime_servers(self) -> list[dict[str, Any]]:
        servers, _ = read_mcp_config(self.mcp_config_path)
        return servers


def _icon_url(*candidates: Any) -> str | None:
    for value in candidates:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("http://") or text.startswith("https://"):
                return text
            continue
        if isinstance(value, dict):
            found = _icon_url(value.get("src"), value.get("url"), value.get("logo_url"), value.get("iconUrl"))
            if found:
                return found
            continue
        if isinstance(value, list):
            found = _icon_url(*value)
            if found:
                return found
    return None


def _official_status(server: dict[str, Any]) -> str | None:
    meta = server.get("_meta")
    if not isinstance(meta, dict):
        return None
    official = meta.get("io.modelcontextprotocol.registry/official")
    if isinstance(official, dict):
        status = official.get("status")
        return str(status) if status else None
    return None


def _marketplace_meta(source: str, source_id: str, version: Any) -> dict[str, Any]:
    return {
        "source": source,
        "sourceId": source_id,
        "version": str(version or "").strip() or None,
        "installedAt": datetime.now(timezone.utc).isoformat(),
    }


def _as_list(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return payload[key]
    return []


def _public_evaluation(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "score": payload.get("score") or payload.get("qualityScore"),
        "grade": payload.get("grade") or payload.get("level"),
        "summary": payload.get("summary") or payload.get("comment"),
    }


def _write_mcp_servers(path: Path, servers: list[dict[str, Any]]) -> None:
    serialized = {}
    for server in servers:
        server_id = str(server["id"])
        payload = {key: value for key, value in server.items() if key not in {"id", "name", "description"}}
        serialized[server_id] = payload
    write_json_atomic(path, {"mcpServers": serialized})
