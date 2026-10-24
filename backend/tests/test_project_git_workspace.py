"""Project 会话的 Git 仓库探测、detached worktree 和环境持久化。"""

from __future__ import annotations

import os
import subprocess
import unittest
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import httpx

from access_layer.home import reset_home_cache, resolve_managed_path
from access_layer.projects.git import (
    diff_for_path,
    inspect_repository,
    list_changes,
    prepare_session_environment,
    remove_managed_worktree,
    handoff_environment,
    GitWorkspaceError,
    prepare_project_session,
)
from access_layer.projects.models import ProjectCreateInput, ProjectRecord, ProjectSessionCreateInput
from access_layer.concurrency import RequestConcurrencyLimiter, ConcurrencyLimitExceeded
from access_layer.projects.store import ProjectStore
from access_layer.sessions.store import SessionStore
from access_layer.sessions.workspace_resolver import resolve_session_workspace
from access_layer.storage.file import FileStorage


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@contextmanager
def git_project():
    with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
        with patch.dict(os.environ, {"K_AGENT_HOME": home}, clear=False):
            reset_home_cache()
            root = Path(project_dir).resolve()
            _git(root, "init")
            _git(root, "config", "user.email", "tests@example.com")
            _git(root, "config", "user.name", "K Agent Tests")
            (root / "hello.txt").write_text("one\n", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "child.txt").write_text("child\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "initial")
            now = datetime.now(timezone.utc)
            project = ProjectRecord(id="project-test", name="test", rootPath=str(root), createdAt=now, updatedAt=now)
            try:
                yield project, Path(home)
            finally:
                reset_home_cache()


class ProjectGitWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_home_cache()

    async def asyncTearDown(self) -> None:
        reset_home_cache()

    async def test_project_registration_does_not_scaffold_plain_directory(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            with patch.dict(os.environ, {"K_AGENT_HOME": home}, clear=False):
                reset_home_cache()
                project = await ProjectStore().create(ProjectCreateInput(rootPath=project_dir))

            self.assertFalse(project.is_git_repository)
            self.assertEqual(list(Path(project_dir).iterdir()), [])

    async def test_git_project_session_uses_detached_worktree_and_persists_binding(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            root = Path(project_dir)
            _git(root, "init")
            _git(root, "config", "user.email", "tests@example.com")
            _git(root, "config", "user.name", "K Agent Tests")
            (root / "hello.txt").write_text("one\n", encoding="utf-8")
            _git(root, "add", "hello.txt")
            _git(root, "commit", "-m", "initial")

            with patch.dict(os.environ, {"K_AGENT_HOME": home}, clear=False):
                reset_home_cache()
                storage = FileStorage(Path(home) / "state")
                project_store = ProjectStore(storage)
                project = await project_store.create(ProjectCreateInput(rootPath=project_dir))
                self.assertTrue(project.is_git_repository)

                environment = prepare_session_environment(project, "session-1", "worktree")
                self.assertEqual(environment.kind, "worktree")
                self.assertTrue(environment.detached)
                self.assertNotEqual(Path(environment.cwd), root)
                self.assertEqual(_git(Path(environment.cwd), "rev-parse", "HEAD"), environment.base_oid)

                store = SessionStore(storage, project_store=project_store)
                session = await store.create_session(
                    session_id="session-1",
                    project_id=project.id,
                    environment=environment,
                )
                self.assertEqual(await resolve_session_workspace(session, project_store), Path(environment.cwd))

                (Path(environment.cwd) / "hello.txt").write_text("two\n", encoding="utf-8")
                changes = list_changes(environment)
                self.assertEqual([(item.path, item.status) for item in changes], [("hello.txt", " M")])
                self.assertIn("-one", diff_for_path(environment, "hello.txt"))
                self.assertIn("+two", diff_for_path(environment, "hello.txt"))

                reloaded = SessionStore(storage, project_store=project_store)
                restored = await reloaded.get("session-1")
                assert restored is not None and restored.environment is not None
                self.assertEqual(restored.environment.cwd, environment.cwd)

                remove_managed_worktree(environment)

    async def test_same_directory_fork_keeps_worktree_ownership_and_resolves(self) -> None:
        with git_project() as (project, home):
            environment = prepare_session_environment(project, "source", "worktree")
            store = SessionStore(FileStorage(home / "state"))
            await store.create_session(session_id="source", project_id=project.id, environment=environment)
            branch = await store.fork_session("source")
            assert branch is not None
            self.assertNotEqual(branch.id, "source")
            self.assertEqual(await resolve_session_workspace(branch), Path(environment.cwd))
            remove_managed_worktree(environment)

    async def test_different_workspaces_parallel_but_shared_workspace_serial(self) -> None:
        limiter = RequestConcurrencyLimiter(3, 0.02)
        async with limiter.protect("one", workspace_key="same"):
            async with limiter.protect("two", workspace_key="different"):
                with self.assertRaises(ConcurrencyLimitExceeded):
                    async with limiter.protect("three", workspace_key="same"):
                        self.fail("Shared cwd must remain locked")
        # 超时必须释放已占用的会话锁和请求槽。
        async with limiter.protect("three", workspace_key="same"):
            pass

    async def test_workspace_resolved_after_session_lock(self) -> None:
        limiter = RequestConcurrencyLimiter(2, 1)
        resolved = asyncio.Event()
        entered = asyncio.Event()
        async def key():
            resolved.set()
            return "workspace"
        async def waiting_run():
            async with limiter.protect("one", workspace_key=key):
                entered.set()
        async with limiter.protect_idle_session("one"):
            task = asyncio.create_task(waiting_run())
            await asyncio.sleep(0)
            self.assertFalse(resolved.is_set())
        await task
        self.assertTrue(entered.is_set())

    async def test_http_create_fork_review_and_handoff_share_one_binding(self) -> None:
        from access_layer.main import create_app
        with git_project() as (fixture_project, home):
            storage = FileStorage(home / "state")
            projects = ProjectStore(storage)
            sessions = SessionStore(storage, project_store=projects)
            app = create_app()
            app.state.project_store = projects
            app.state.session_store = sessions
            app.state.request_limiter = RequestConcurrencyLimiter(3, 1)
            app.state.terminals = SimpleNamespace(close_session=AsyncMock())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/projects", json={"rootPath": fixture_project.root_path})
                self.assertEqual(response.status_code, 200, response.text)
                project = response.json()
                response = await client.post(f"/api/projects/{project['id']}/sessions", json={"environment": "worktree"})
                self.assertEqual(response.status_code, 200, response.text)
                session = response.json()
                cwd = Path(session["environment"]["cwd"])
                (cwd / "hello.txt").write_text("task change\n")
                _git(cwd, "commit", "-am", "task")
                response = await client.get(f"/api/sessions/{session['id']}/changes")
                self.assertEqual([item["path"] for item in response.json()["files"]], ["hello.txt"])
                response = await client.post(f"/api/sessions/{session['id']}/fork", json={"environment": "same-directory"})
                self.assertEqual(response.status_code, 200, response.text)
                branch = response.json()
                response = await client.get(f"/api/sessions/{branch['id']}/workspace")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(resolve_managed_path(response.json()["root"]), cwd)
                response = await client.post(f"/api/sessions/{session['id']}/handoff", json={"target": "local"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["environment"]["kind"], "local")
                self.assertEqual((Path(project["rootPath"]) / "hello.txt").read_text(), "task change\n")
                self.assertTrue(cwd.exists())
                app.state.terminals.close_session.assert_awaited_once_with(session["id"])
                response = await client.post(f"/api/sessions/{session['id']}/handoff", json={"target": "worktree"})
                self.assertEqual(response.status_code, 200, response.text)


class GitLifecycleTests(unittest.TestCase):
    def test_review_includes_committed_changes_and_staged_deletion(self) -> None:
        with git_project() as (project, _):
            environment = prepare_session_environment(project, "review", "worktree")
            cwd = Path(environment.cwd)
            (cwd / "hello.txt").write_text("two\n", encoding="utf-8")
            _git(cwd, "commit", "-am", "task change")
            self.assertIn("hello.txt", [change.path for change in list_changes(environment)])
            self.assertIn("+two", diff_for_path(environment, "hello.txt"))
            _git(cwd, "rm", "hello.txt")
            self.assertIn("-one", diff_for_path(environment, "hello.txt"))
            with self.assertRaises(GitWorkspaceError):
                diff_for_path(environment, "../outside")
            remove_managed_worktree(environment)

    def test_handoff_fast_forwards_local_and_preserves_worktree_for_return(self) -> None:
        with git_project() as (project, _):
            environment = prepare_session_environment(project, "handoff", "worktree")
            cwd = Path(environment.cwd)
            (cwd / "hello.txt").write_text("two\n", encoding="utf-8")
            _git(cwd, "commit", "-am", "task change")
            local = handoff_environment(project, "handoff", environment, "local")
            self.assertEqual((Path(project.root_path) / "hello.txt").read_text(), "two\n")
            self.assertTrue(cwd.is_dir())
            self.assertEqual(local.base_oid, environment.base_oid)
            returned = handoff_environment(project, "handoff", local, "worktree")
            self.assertEqual(returned.cwd, environment.cwd)
            remove_managed_worktree(environment)

    def test_handoff_rejects_dirty_target_without_changing_either_checkout(self) -> None:
        with git_project() as (project, _):
            environment = prepare_session_environment(project, "handoff", "worktree")
            root = Path(project.root_path)
            (root / "hello.txt").write_text("local edits\n")
            with self.assertRaises(GitWorkspaceError):
                handoff_environment(project, "handoff", environment, "local")
            self.assertEqual((root / "hello.txt").read_text(), "local edits\n")
            self.assertTrue(Path(environment.cwd).is_dir())
            remove_managed_worktree(environment)

    def test_working_tree_start_preserves_staged_unstaged_and_untracked(self) -> None:
        with git_project() as (project, _):
            root = Path(project.root_path)
            (root / "hello.txt").write_text("staged\n")
            _git(root, "add", "hello.txt")
            (root / "hello.txt").write_text("unstaged\n")
            (root / "new.bin").write_bytes(b"\x00\xff\x01")
            environment = prepare_project_session(project, "snapshot", ProjectSessionCreateInput(environment="worktree", startingState={"type": "working-tree"}))
            cwd = Path(environment.cwd)
            self.assertEqual(_git(cwd, "show", ":hello.txt"), "staged")
            self.assertEqual((cwd / "hello.txt").read_text(), "unstaged\n")
            self.assertEqual((cwd / "new.bin").read_bytes(), b"\x00\xff\x01")
            self.assertEqual(_git(root, "status", "--porcelain"), _git(cwd, "status", "--porcelain"))
            remove_managed_worktree(environment)

    def test_local_handoff_copies_dirty_state_without_removing_source_edits(self) -> None:
        with git_project() as (project, _):
            root = Path(project.root_path)
            local = prepare_session_environment(project, "local", "local")
            (root / "hello.txt").write_text("uncommitted\n")
            (root / "notes.txt").write_text("notes\n")
            worktree = handoff_environment(project, "local", local, "worktree")
            self.assertEqual((Path(worktree.cwd) / "hello.txt").read_text(), "uncommitted\n")
            self.assertEqual((root / "hello.txt").read_text(), "uncommitted\n")
            self.assertEqual((Path(worktree.cwd) / "notes.txt").read_text(), "notes\n")
            remove_managed_worktree(worktree)

    def test_explicit_branch_attaches_only_when_requested(self) -> None:
        with git_project() as (project, _):
            options = ProjectSessionCreateInput(environment="worktree", startingState={"type": "branch", "branchName": "task-branch"})
            with self.assertRaises(GitWorkspaceError):
                prepare_project_session(project, "branch", options)
            options.starting_state.on_missing = "create-branch"
            environment = prepare_project_session(project, "branch", options)
            self.assertFalse(environment.detached)
            self.assertEqual(environment.branch_ref, "refs/heads/task-branch")
            self.assertEqual(_git(Path(environment.cwd), "symbolic-ref", "HEAD"), "refs/heads/task-branch")
            remove_managed_worktree(environment)

    def test_subdirectory_project_keeps_cwd_and_relative_review_paths(self) -> None:
        with git_project() as (project, _):
            project.root_path = str(Path(project.root_path) / "nested")
            environment = prepare_session_environment(project, "nested", "worktree")
            self.assertEqual(Path(environment.cwd).name, "nested")
            (Path(environment.cwd) / "child.txt").write_text("changed\n")
            self.assertEqual([change.path for change in list_changes(environment)], ["child.txt"])
            self.assertIn("+changed", diff_for_path(environment, "child.txt"))
            remove_managed_worktree(environment)


class RepositoryInspectionTests(unittest.TestCase):
    def test_plain_directory_is_not_git_repository(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertFalse(inspect_repository(directory).is_git_repository)


if __name__ == "__main__":
    unittest.main()
