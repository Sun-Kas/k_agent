"""把 Session 的持久环境绑定解析为唯一 cwd。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from access_layer.home import session_workspace_dir, session_worktree_dir
from access_layer.projects.git import inspect_repository


async def resolve_session_workspace(session: Any, project_store: Any | None = None) -> Path:
    """解析执行目录，并对托管 worktree 做路径边界校验。"""

    environment = getattr(session, "environment", None)
    if environment is not None:
        cwd = Path(environment.cwd).expanduser().resolve()
        if environment.project_id != session.project_id:
            raise ValueError("Session environment does not belong to its Project")
        if environment.kind == "worktree":
            expected = session_worktree_dir(
                environment.project_id, environment.worktree_id or session.id
            ).resolve()
            if cwd != expected and expected not in cwd.parents:
                raise ValueError("Managed worktree path does not match the session binding")
        if not cwd.is_dir():
            raise ValueError("Session workspace no longer exists")
        if environment.git_common_dir:
            repository = await asyncio.to_thread(inspect_repository, cwd)
            if repository.git_common_dir != environment.git_common_dir:
                raise ValueError("Session workspace repository no longer matches its binding")
        return cwd

    if session.project_id:
        if project_store is None:
            raise ValueError("Project store is unavailable")
        project = await project_store.get(session.project_id)
        if project is None:
            raise ValueError("Project not found for this session")
        return Path(project.root_path).resolve()

    workspace = session_workspace_dir(session.id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace
