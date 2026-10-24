"""成果合入和冲突解决 API；与 Agent Run 共用会话及工作目录锁。"""

import asyncio
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from access_layer.concurrency import SessionAlreadyRunning
from access_layer.projects.git import GitWorkspaceError
from access_layer.projects.integration import (
    abort_integration, commit_workspace, conflict_file, continue_integration,
    integration_lock_paths, read_integration, resolve_conflict, start_integration,
)
from access_layer.sessions.workspace_resolver import resolve_session_workspace


class CommitInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ConflictInput(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    choice: Literal["manual", "ours", "theirs", "delete"]
    expectedHash: str = Field(min_length=64, max_length=64)
    content: str = Field(default="", max_length=1024 * 1024)


def build_integration_router(projects, sessions, limiter) -> APIRouter:
    router = APIRouter(prefix="/api/sessions", tags=["integration"])

    async def context(session_id):
        session = await sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        project = await projects.get(session.project_id) if session.project_id else None
        if project is None or session.environment is None:
            raise HTTPException(status_code=400, detail="会话尚未绑定项目工作区")
        return session, project

    async def control(session_id, operation, *arguments):
        try:
            async with limiter.protect_idle_session(session_id):
                session, project = await context(session_id)
                if session.open_interrupt_ids:
                    raise HTTPException(status_code=409, detail="请先处理会话中的待审批操作")
                await resolve_session_workspace(session, projects)
                paths = await asyncio.to_thread(integration_lock_paths, project, session_id, session.environment)
                # 子目录 Project 的运行锁以 cwd 为键，发布时也覆盖同 checkout 下的别名。
                for summary in await sessions.list_summaries(project_id=project.id):
                    if summary.environment:
                        cwd = Path(summary.environment.cwd).resolve()
                        if any(cwd.is_relative_to(Path(path)) or Path(path).is_relative_to(cwd) for path in paths):
                            paths.append(str(cwd))
                async with limiter.protect_idle_workspaces(paths):
                    if operation == "start":
                        return await asyncio.to_thread(start_integration, project, session_id, session.environment)
                    if operation == "commit":
                        message = arguments[0].strip()
                        if not message:
                            raise HTTPException(status_code=400, detail="提交说明不能为空")
                        oid = await asyncio.to_thread(commit_workspace, session.environment, message)
                        return {"oid": oid}
                    function = {"continue": continue_integration, "abort": abort_integration, "resolve": resolve_conflict}[operation]
                    return await asyncio.to_thread(function, project, session_id, *arguments)
        except (SessionAlreadyRunning, GitWorkspaceError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/{session_id}/integration")
    async def status(session_id: str):
        _, project = await context(session_id)
        try:
            return await asyncio.to_thread(read_integration, project, session_id)
        except (GitWorkspaceError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{session_id}/integration")
    async def start(session_id: str):
        return await control(session_id, "start")

    @router.post("/{session_id}/integration/continue")
    async def proceed(session_id: str):
        return await control(session_id, "continue")

    @router.post("/{session_id}/integration/abort")
    async def abort(session_id: str):
        return await control(session_id, "abort")

    @router.get("/{session_id}/integration/file")
    async def read_file(session_id: str, path: str):
        _, project = await context(session_id)
        try:
            return await asyncio.to_thread(conflict_file, project, session_id, path)
        except (GitWorkspaceError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.put("/{session_id}/integration/file")
    async def resolve(session_id: str, payload: ConflictInput):
        return await control(session_id, "resolve", payload.model_dump())

    @router.post("/{session_id}/git/commit")
    async def commit(session_id: str, payload: CommitInput):
        return await control(session_id, "commit", payload.message)

    return router
