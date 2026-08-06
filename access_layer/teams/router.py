"""FastAPI routes for supervising durable Agent Teams from the workbench."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from access_layer.teams.models import (
    TeamCommandInput,
    TeamCreateInput,
    TeamMessageInput,
    TeamTaskCreateInput,
)
from access_layer.teams.runtime import TeamRuntime
from access_layer.teams.workspace import list_team_workspace, read_team_workspace_file


def build_team_router(runtime: TeamRuntime) -> APIRouter:
    """Bind API handlers to one application-owned Team Runtime instance."""

    router = APIRouter(prefix="/api/teams", tags=["agent-teams"])

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
            return await runtime.store.events_for_task(team_id, taskId, limit=bounded)
        if afterSeq <= 0:
            # Initial hydration wants the newest window, not the oldest page.
            return await runtime.store.events_tail(team_id, limit=bounded)
        return await runtime.store.events_after(team_id, max(0, afterSeq), limit=min(bounded, 200))

    @router.get("/{team_id}/stream")
    async def stream_events(team_id: str, request: Request, afterSeq: int = 0) -> StreamingResponse:
        if await runtime.store.get_team(team_id) is None:
            raise HTTPException(status_code=404, detail="Team not found")

        async def event_stream():
            # Polling the durable event log keeps reconnect semantics correct
            # across Access Layer restarts and avoids process-local subscribers.
            cursor = max(0, afterSeq)
            idle_ticks = 0
            while not await request.is_disconnected():
                events = await runtime.store.events_after(team_id, cursor)
                if events:
                    idle_ticks = 0
                    for event in events:
                        cursor = int(event["seq"])
                        yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
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
