"""公开 /api/marketplace/*；浏览器只打这里，不直连外部注册表。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from access_layer.marketplace.service import MarketplaceService
from access_layer.marketplace.skillhub import SkillHubError


class McpInstallInput(BaseModel):
    id: str | None = None
    enabled: bool = True
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class SkillInstallInput(BaseModel):
    id: str | None = None
    enabled: bool = True


def build_marketplace_router() -> APIRouter:
    router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])

    def service(request: Request) -> MarketplaceService:
        return request.app.state.marketplace

    @router.get("/mcp")
    async def list_mcp(request: Request, q: str = "", page: int = 1, pageSize: int = 24):
        return await service(request).list_mcp(q=q, page=page, page_size=pageSize)

    @router.get("/mcp/item")
    async def get_mcp(request: Request, sourceId: str):
        return await service(request).mcp_item(sourceId)

    @router.post("/mcp/item/preview")
    async def preview_mcp(request: Request, sourceId: str):
        return await service(request).preview_mcp(sourceId)

    @router.post("/mcp/item/install")
    async def install_mcp(request: Request, sourceId: str, payload: McpInstallInput):
        result = await service(request).install_mcp(
            sourceId,
            local_id=payload.id,
            enabled=payload.enabled,
            env=payload.env,
            headers=payload.headers,
        )
        try:
            await request.app.state.agent_backend_client.post_json("/internal/mcp/reload")
        except Exception:
            result["warnings"] = ["已写入本机配置，但 Agent Backend 重载 MCP 失败"]
        return result

    @router.post("/mcp/{local_id}/uninstall")
    async def uninstall_mcp(request: Request, local_id: str):
        result = await service(request).uninstall_mcp(local_id)
        try:
            await request.app.state.agent_backend_client.post_json("/internal/mcp/reload")
        except Exception:
            result["warnings"] = ["已从本机移除，但 Agent Backend 重载 MCP 失败"]
        return result

    @router.get("/skills")
    async def list_skills(
        request: Request,
        q: str = "",
        category: str = "",
        sortBy: str = "",
        page: int = 1,
        pageSize: int = 20,
    ):
        return await service(request).list_skills(
            q=q, category=category, sort_by=sortBy, page=page, page_size=pageSize
        )

    @router.get("/skills/top")
    async def top_skills(request: Request):
        return await service(request).top_skills()

    @router.get("/skills/categories")
    async def skill_categories(request: Request):
        return await service(request).categories()

    @router.get("/skills/{slug}/evaluation")
    async def skill_evaluation(request: Request, slug: str):
        try:
            return await service(request).skill_evaluation(slug)
        except SkillHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/skills/{slug}/versions")
    async def skill_versions(request: Request, slug: str):
        try:
            return await service(request).skill_versions(slug)
        except SkillHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/skills/{slug}")
    async def get_skill(request: Request, slug: str):
        try:
            return await service(request).skill_item(slug)
        except SkillHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/skills/{slug}/preview")
    async def preview_skill(request: Request, slug: str):
        return await service(request).preview_skill(slug)

    @router.post("/skills/{slug}/install")
    async def install_skill(request: Request, slug: str, payload: SkillInstallInput):
        return await service(request).install_skill(slug, local_id=payload.id, enabled=payload.enabled)

    @router.post("/skills/{local_id}/uninstall")
    async def uninstall_skill(request: Request, local_id: str):
        return await service(request).uninstall_skill(local_id)

    return router
