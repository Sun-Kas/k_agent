"""Agent Teams 公开 HTTP 路由：工作台监督持久化团队（状态在 Access Layer）。

在请求链路中的角色：CRUD / 命令 / 任务 / 邮箱 / 事件查询与 SSE 订阅；
调度与后端调用由 TeamRuntime 完成，本模块只绑定 FastAPI 与 TeamStore。

服务边界：事件流通过轮询 SQLite 事件日志实现，以便 Access Layer 重启后
仍可按 seq 续订，而不依赖进程内订阅者。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from access_layer.teams.models import (
    TeamCommandInput,
    TeamApprovalResumeInput,
    TeamCreateInput,
    TeamMessageInput,
    TeamTaskCreateInput,
)
from access_layer.teams.runtime import TeamRuntime
from access_layer.teams.workspace import list_team_workspace, read_team_workspace_file


def build_team_router(runtime: TeamRuntime) -> APIRouter:
    """把 `/api/teams*` 处理器绑定到应用拥有的 TeamRuntime（实际多用其 store）。"""

    router = APIRouter(prefix="/api/teams", tags=["agent-teams"])

    def public_event(event: dict) -> dict:
        """Team checkpoint 留在 SQLite；事件 API 只返回审批卡展示字段。"""

        payload = dict(event.get("payload") or {})
        payload.pop("_checkpoint", None)
        return {**event, "payload": payload}

    @router.get("")
    async def list_teams() -> list[dict]:
        return await runtime.store.list_teams()

    @router.post("")
    async def create_team(payload: TeamCreateInput) -> dict:
        try:
            return await runtime.store.create_team(
                payload, runtime.store.database_path.parent
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/{team_id}")
    async def get_team(team_id: str) -> dict:
        team = await runtime.store.get_team(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        return team

    @router.get("/{team_id}/workspace")
    async def get_team_workspace(team_id: str) -> dict:
        team = await runtime.store.get_team(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        try:
            listing = list_team_workspace(team["workspaceDir"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "teamId": team_id,
            "root": listing.root,
            "files": [
                {
                    "path": item.path,
                    "name": item.name,
                    "size": item.size,
                    "modifiedAt": item.modified_at,
                }
                for item in listing.files
            ],
        }

    @router.get("/{team_id}/workspace/file")
    async def get_team_workspace_file(team_id: str, path: str) -> dict:
        team = await runtime.store.get_team(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        try:
            payload = read_team_workspace_file(team["workspaceDir"], path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "teamId": team_id,
            "path": payload.path,
            "name": payload.name,
            "content": payload.content,
            "truncated": payload.truncated,
            "binary": payload.binary,
            "size": payload.size,
        }

    @router.get("/{team_id}/events")
    async def get_events(
        team_id: str,
        afterSeq: int = 0,
        limit: int = 200,
        taskId: str | None = None,
    ) -> list[dict]:
        if await runtime.store.get_team(team_id) is None:
            raise HTTPException(status_code=404, detail="Team not found")
        bounded = max(1, min(limit, 5000))
        if taskId:
            return [
                public_event(event)
                for event in await runtime.store.events_for_task(team_id, taskId, limit=bounded)
            ]
        if afterSeq <= 0:
            # Initial hydration wants the newest window, not the oldest page.
            return [
                public_event(event)
                for event in await runtime.store.events_tail(team_id, limit=bounded)
            ]
        return [
            public_event(event)
            for event in await runtime.store.events_after(
                team_id, max(0, afterSeq), limit=min(bounded, 200)
            )
        ]

    @router.get("/{team_id}/stream")
    async def stream_events(team_id: str, request: Request, afterSeq: int = 0) -> StreamingResponse:
        if await runtime.store.get_team(team_id) is None:
            raise HTTPException(status_code=404, detail="Team not found")

        async def event_stream():
            # 轮询持久事件日志：重启后续订语义正确，且不依赖进程内订阅者。
            cursor = max(0, afterSeq)
            idle_ticks = 0
            while not await request.is_disconnected():
                events = await runtime.store.events_after(team_id, cursor)
                if events:
                    idle_ticks = 0
                    for event in events:
                        cursor = int(event["seq"])
                        yield f"data: {json.dumps(public_event(event), ensure_ascii=False, separators=(',', ':'))}\n\n"
                else:
                    idle_ticks += 1
                    if idle_ticks >= 15:
                        yield ": heartbeat\n\n"
                        idle_ticks = 0
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/{team_id}/commands")
    async def command_team(team_id: str, payload: TeamCommandInput) -> dict:
        team = await runtime.store.command(team_id, payload.command)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        return team

    @router.post("/{team_id}/approvals/{approval_id}/resume")
    async def resume_approval(
        team_id: str, approval_id: str, payload: TeamApprovalResumeInput
    ) -> dict:
        try:
            return await runtime.resume_approval(
                team_id,
                approval_id,
                action=payload.action,
                scope=payload.scope,
                answers=payload.answers,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Team not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{team_id}/tasks")
    async def create_task(team_id: str, payload: TeamTaskCreateInput) -> dict:
        team = await runtime.store.create_task(team_id, payload)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        return team

    @router.post("/{team_id}/messages")
    async def send_message(team_id: str, payload: TeamMessageInput) -> dict:
        team = await runtime.store.send_message(
            team_id,
            payload.sender_id,
            payload.recipient_id,
            payload.message_type,
            payload.content,
            payload.artifact_ids,
        )
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        return team

    return router
