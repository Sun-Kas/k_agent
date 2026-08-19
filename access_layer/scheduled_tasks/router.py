"""REST API for creating and observing scheduled Work tasks."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from backend.api.schemas import SessionCapabilities, SessionState

from access_layer.scheduled_tasks.models import (
    ScheduledApprovalResumeInput,
    ScheduledRunOutput,
    ScheduledTaskInput,
    ScheduledTaskOutput,
)
from access_layer.scheduled_tasks.schedule import ScheduleValidationError
from access_layer.scheduled_tasks.store import ScheduledTaskConflict


def build_scheduled_task_router(runtime) -> APIRouter:
    router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled-tasks"])

    @router.get("", response_model=list[ScheduledTaskOutput])
    async def list_tasks():
        return await runtime.store.list()

    @router.post("", response_model=ScheduledTaskOutput, status_code=201)
    async def create_task(payload: ScheduledTaskInput):
        try:
            task = await runtime.store.create(payload)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.wake()
        return task

    @router.get("/{task_id}", response_model=ScheduledTaskOutput)
    async def get_task(task_id: str):
        task = await runtime.store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        return task

    @router.put("/{task_id}", response_model=ScheduledTaskOutput)
    async def update_task(task_id: str, payload: ScheduledTaskInput):
        try:
            task = await runtime.store.update(task_id, payload)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        runtime.wake()
        return task

    async def _set_status(task_id: str, status: str):
        try:
            task = await runtime.store.set_status(task_id, status)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        runtime.wake()
        return task

    @router.post("/{task_id}/pause", response_model=ScheduledTaskOutput)
    async def pause_task(task_id: str):
        return await _set_status(task_id, "paused")

    @router.post("/{task_id}/resume", response_model=ScheduledTaskOutput)
    async def resume_task(task_id: str):
        return await _set_status(task_id, "active")

    @router.delete("/{task_id}", status_code=204)
    async def delete_task(task_id: str):
        try:
            deleted = await runtime.store.delete(task_id)
        except ScheduledTaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        runtime.wake()
        return Response(status_code=204)

    @router.get("/{task_id}/runs", response_model=list[ScheduledRunOutput])
    async def list_runs(task_id: str, limit: int = Query(default=50, ge=1, le=200)):
        if await runtime.store.get(task_id) is None:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        return await runtime.store.list_runs(task_id, limit)

    @router.get("/{task_id}/runs/{run_id}/session", response_model=SessionState)
    async def get_run_session(task_id: str, run_id: str):
        """Read scheduled output in-place without publishing it as a normal chat."""
        run = await runtime.store.get_run(task_id, run_id)
        if run is None or not run.get("sessionId") or runtime._session_store is None:
            raise HTTPException(status_code=404, detail="Scheduled run session not found")
        session = await runtime._session_store.get(run["sessionId"])
        if session is None:
            raise HTTPException(status_code=404, detail="Scheduled run session not found")
        open_interrupts = await runtime._session_store.list_open_interrupts(session.id)
        return SessionState(
            sessionId=session.id, messages=session.messages, trace=session.trace,
            tasks=session.tasks, thinking=session.thinking, events=session.events,
            openInterrupts=open_interrupts,
            capabilities=(
                SessionCapabilities(
                    mcpServerIds=session.mcp_server_ids,
                    skillIds=session.skill_ids,
                )
                if session.mcp_server_ids is not None and session.skill_ids is not None
                else None
            ),
        )

    @router.post("/{task_id}/runs/{run_id}/resume")
    async def resume_run(
        task_id: str, run_id: str, payload: ScheduledApprovalResumeInput
    ) -> dict:
        try:
            return await runtime.resume_approval(
                task_id, run_id,
                interrupt_id=payload.interrupt_id,
                action=payload.action,
                scope=payload.scope,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Scheduled run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{task_id}/run-now", status_code=202)
    async def run_now(task_id: str):
        try:
            claimed = await runtime.run_now(task_id)
        except ScheduledTaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if claimed is None:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        return claimed["run"]

    return router
