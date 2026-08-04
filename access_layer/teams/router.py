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

    @router.get("/{team_id}/events")
    async def get_events(team_id: str, afterSeq: int = 0) -> list[dict]:
        if await runtime.store.get_team(team_id) is None:
            raise HTTPException(status_code=404, detail="Team not found")
        return await runtime.store.events_after(team_id, max(0, afterSeq))

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
