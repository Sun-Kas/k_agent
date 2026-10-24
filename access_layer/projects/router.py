"""Project 公开 HTTP 路由：`/api/projects*`。

在请求链路中的角色：Project CRUD + 归属会话的列出/创建 + 只读工作区预览。
会话归属的迁移（`POST /api/sessions/{id}/project`）留在 main.py 里，因为它
同时需要 SessionStore 的迁移方法和这里的存在性校验，放哪边都要互相依赖，
main.py 本来就持有两个 store 的引用，放那里更顺。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from access_layer.projects.git import (
    GitWorkspaceError,
    inspect_repository,
    prepare_project_session,
    remove_managed_worktree,
    _git,
)
from access_layer.projects.models import (
    ProjectCreateInput,
    ProjectSessionCreateInput,
    ProjectUpdateInput,
)
from access_layer.projects.store import ProjectStore, ProjectValidationError
from access_layer.projects.workspace import list_project_workspace, read_project_workspace_file
from access_layer.sessions.store import SessionStore


def build_project_router(project_store: ProjectStore, session_store: SessionStore) -> APIRouter:
    """把 `/api/projects*` 处理器绑定到 lifespan 持有的 ProjectStore/SessionStore。"""

    router = APIRouter(prefix="/api/projects", tags=["projects"])

    async def _get_or_404(project_id: str):
        project = await project_store.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    @router.get("")
    async def list_projects() -> list[dict]:
        projects = await project_store.list()
        return [project.model_dump(by_alias=True, mode="json") for project in projects]

    @router.post("")
    async def create_project(payload: ProjectCreateInput) -> dict:
        try:
            project = await project_store.create(payload)
        except ProjectValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return project.model_dump(by_alias=True, mode="json")

    @router.get("/{project_id}")
    async def get_project(project_id: str) -> dict:
        project = await _get_or_404(project_id)
        return project.model_dump(by_alias=True, mode="json")

    @router.patch("/{project_id}")
    async def update_project(project_id: str, payload: ProjectUpdateInput) -> dict:
        try:
            project = await project_store.update(project_id, payload)
        except ProjectValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project = await project_store.get(project_id)
        return project.model_dump(by_alias=True, mode="json")

    @router.delete("/{project_id}")
    async def delete_project(project_id: str) -> dict[str, bool]:
        # 只删索引记录；root_path 指向的磁盘目录永远不被这个接口触碰，
        # 归属该 Project 的会话簿记目前也原样保留（不是本版本要处理的级联行为）。
        deleted = await project_store.delete(project_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Project not found")
        return {"deleted": True}

    @router.get("/{project_id}/sessions")
    async def list_project_sessions(project_id: str) -> list[dict]:
        await _get_or_404(project_id)
        summaries = await session_store.list_summaries(project_id=project_id)
        return [summary.model_dump(by_alias=True, mode="json") for summary in summaries]

    @router.post("/{project_id}/sessions")
    async def create_project_session(
        project_id: str, payload: ProjectSessionCreateInput | None = None
    ) -> dict:
        project = await _get_or_404(project_id)
        options = payload or ProjectSessionCreateInput()
        session_id = str(uuid.uuid4())
        try:
            environment = await asyncio.to_thread(
                prepare_project_session,
                project,
                session_id,
                options,
            )
        except GitWorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            session = await session_store.create_session(
                session_id=session_id,
                project_id=project.id,
                environment=environment,
            )
        except Exception:
            # Git worktree 已创建但会话落盘失败时立即回滚，避免留下孤儿目录。
            await asyncio.to_thread(remove_managed_worktree, environment)
            raise
        return {
            "id": session.id,
            "title": session.title,
            "updatedAt": session.updated_at.isoformat(),
            "messageCount": len(session.messages),
            "projectId": session.project_id,
            "environment": environment.model_dump(by_alias=True, mode="json"),
        }

    @router.get("/{project_id}/repository")
    async def get_project_repository(project_id: str) -> dict:
        project = await _get_or_404(project_id)
        repository = await asyncio.to_thread(inspect_repository, project.root_path)
        result = repository.model_dump(by_alias=True, mode="json")
        result["targetBranch"] = project.target_branch
        if repository.is_git_repository:
            branches = await asyncio.to_thread(_git, Path(project.root_path), "for-each-ref", "--format=%(refname:short)", "refs/heads/")
            result["branches"] = branches.stdout.splitlines()
        else:
            result["branches"] = []
        return result

    @router.get("/{project_id}/workspace")
    async def get_project_workspace(project_id: str) -> dict:
        project = await _get_or_404(project_id)
        try:
            listing = list_project_workspace(project.root_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "projectId": project_id,
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

    @router.get("/{project_id}/workspace/file")
    async def get_project_workspace_file(project_id: str, path: str) -> dict:
        project = await _get_or_404(project_id)
        try:
            payload = read_project_workspace_file(project.root_path, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "projectId": project_id,
            "path": payload.path,
            "name": payload.name,
            "content": payload.content,
            "truncated": payload.truncated,
            "binary": payload.binary,
            "size": payload.size,
        }

    return router
