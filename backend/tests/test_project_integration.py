"""共享目录默认策略、分叉成果汇总、隔离冲突编辑与发布前状态校验。"""

from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from access_layer.concurrency import RequestConcurrencyLimiter
from access_layer.projects.git import (
    GitWorkspaceError, prepare_project_session, prepare_session_environment,
)
from access_layer.projects.integration import (
    abort_integration, conflict_file, continue_integration, read_integration,
    resolve_conflict, start_integration,
)
import access_layer.projects.integration as integration_module
from access_layer.projects.integration_router import build_integration_router
from access_layer.projects.models import ProjectCreateInput, ProjectSessionCreateInput, ProjectUpdateInput
from access_layer.projects.router import build_project_router
from access_layer.projects.store import ProjectStore, ProjectValidationError
from access_layer.sessions.store import SessionStore
from access_layer.storage import FileStorage
from backend.tests.test_project_git_workspace import _git, git_project


def target(project):
    project.target_branch = _git(Path(project.root_path), "symbolic-ref", "--short", "HEAD")


def commit_file(cwd, path, content):
    (Path(cwd) / path).write_text(content)
    _git(Path(cwd), "add", "--", path)
    _git(Path(cwd), "commit", "-m", path)


class ProjectIntegrationTests(unittest.TestCase):
    def test_default_sessions_share_directory_without_creating_worktrees(self):
        with git_project() as (project, home):
            first = prepare_project_session(project, "one", ProjectSessionCreateInput())
            second = prepare_project_session(project, "two", ProjectSessionCreateInput())
            self.assertEqual(first.kind, "local")
            self.assertEqual(first.cwd, second.cwd)
            (Path(first.cwd) / "shared.txt").write_text("visible")
            self.assertEqual((Path(second.cwd) / "shared.txt").read_text(), "visible")
            self.assertFalse((home / "state" / "projects" / project.id / "worktrees").exists())
            self.assertEqual(_git(Path(project.root_path), "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_diverged_sessions_merge_into_one_target_branch(self):
        with git_project() as (project, _):
            target(project)
            first = prepare_session_environment(project, "one", "worktree")
            second = prepare_session_environment(project, "two", "worktree")
            commit_file(first.cwd, "login.txt", "login\n")
            commit_file(second.cwd, "payment.txt", "payment\n")
            self.assertEqual(start_integration(project, "one", first)["status"], "completed")
            result = start_integration(project, "two", second)
            self.assertEqual(result["status"], "completed")
            root = Path(project.root_path)
            self.assertEqual((root / "login.txt").read_text(), "login\n")
            self.assertEqual((root / "payment.txt").read_text(), "payment\n")
            self.assertEqual(len(_git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
            self.assertTrue(Path(first.cwd).exists())
            self.assertTrue(Path(second.cwd).exists())

    def make_conflict(self, project):
        target(project)
        environment = prepare_session_environment(project, "conflict", "worktree")
        commit_file(environment.cwd, "hello.txt", "session\n")
        commit_file(project.root_path, "hello.txt", "target\n")
        state = start_integration(project, "conflict", environment)
        self.assertEqual(state["status"], "conflicts")
        self.assertEqual(state["conflicts"], ["hello.txt"])
        return environment, state

    def test_conflict_resolution_survives_reload_and_preserves_original_workspaces(self):
        with git_project() as (project, _):
            environment, _ = self.make_conflict(project)
            file = conflict_file(project, "conflict", "hello.txt")
            self.assertIn("<<<<<<<", file["content"])
            with self.assertRaises(GitWorkspaceError):
                resolve_conflict(project, "conflict", {"path": "hello.txt", "choice": "manual", "expectedHash": "0" * 64, "content": "resolved\n"})
            with self.assertRaises(GitWorkspaceError):
                resolve_conflict(project, "conflict", {"path": "hello.txt", "choice": "manual", "expectedHash": file["hash"], "content": file["content"]})
            resolve_conflict(project, "conflict", {"path": "hello.txt", "choice": "manual", "expectedHash": file["hash"], "content": "resolved\n"})
            self.assertEqual(read_integration(project, "conflict")["conflicts"], [])
            self.assertEqual((Path(project.root_path) / "hello.txt").read_text(), "target\n")
            self.assertEqual(continue_integration(project, "conflict")["status"], "completed")
            self.assertEqual((Path(project.root_path) / "hello.txt").read_text(), "resolved\n")
            self.assertEqual((Path(environment.cwd) / "hello.txt").read_text(), "session\n")

    def test_target_change_during_resolution_rejects_publish_and_abort_preserves_target(self):
        with git_project() as (project, _):
            environment, state = self.make_conflict(project)
            file = conflict_file(project, "conflict", "hello.txt")
            resolve_conflict(project, "conflict", {"path": "hello.txt", "choice": "theirs", "expectedHash": file["hash"]})
            commit_file(project.root_path, "later.txt", "external commit\n")
            before = _git(Path(project.root_path), "rev-parse", "HEAD")
            with self.assertRaises(GitWorkspaceError):
                continue_integration(project, "conflict")
            self.assertEqual(_git(Path(project.root_path), "rev-parse", "HEAD"), before)
            self.assertEqual(abort_integration(project, "conflict")["status"], "aborted")
            self.assertEqual((Path(project.root_path) / "hello.txt").read_text(), "target\n")
            self.assertTrue(Path(environment.cwd).exists())
            self.assertFalse(Path(state["workspace"]).exists())

    def test_dirty_target_is_never_overwritten(self):
        with git_project() as (project, _):
            target(project)
            environment = prepare_session_environment(project, "dirty", "worktree")
            commit_file(environment.cwd, "task.txt", "task\n")
            (Path(project.root_path) / "hello.txt").write_text("uncommitted user changes\n")
            with self.assertRaises(GitWorkspaceError):
                start_integration(project, "dirty", environment)
            self.assertEqual((Path(project.root_path) / "hello.txt").read_text(), "uncommitted user changes\n")
            self.assertFalse((Path(project.root_path) / "task.txt").exists())

    def test_target_branch_not_checked_out_is_updated_without_switching_user_branch(self):
        with git_project() as (project, _):
            root = Path(project.root_path)
            original = _git(root, "symbolic-ref", "HEAD")
            _git(root, "branch", "release")
            project.target_branch = "release"
            environment = prepare_session_environment(project, "release", "worktree")
            commit_file(environment.cwd, "release.txt", "release\n")
            self.assertEqual(start_integration(project, "release", environment)["status"], "completed")
            self.assertEqual(_git(root, "show", "release:release.txt"), "release")
            self.assertEqual(_git(root, "symbolic-ref", "HEAD"), original)
            self.assertFalse((root / "release.txt").exists())

    def test_binary_conflict_can_keep_session_version(self):
        with git_project() as (project, _):
            target(project)
            environment = prepare_session_environment(project, "binary", "worktree")
            for cwd, content in ((environment.cwd, b"\0session"), (project.root_path, b"\0target")):
                (Path(cwd) / "hello.txt").write_bytes(content)
                _git(Path(cwd), "commit", "-am", "binary")
            self.assertEqual(start_integration(project, "binary", environment)["status"], "conflicts")
            file = conflict_file(project, "binary", "hello.txt")
            self.assertTrue(file["binary"])
            resolve_conflict(project, "binary", {"path": "hello.txt", "expectedHash": file["hash"], "choice": "theirs"})
            self.assertEqual(continue_integration(project, "binary")["status"], "completed")
            self.assertEqual((Path(project.root_path) / "hello.txt").read_bytes(), b"\0session")

    def test_resume_after_commit_saved_but_operation_state_write_failed(self):
        with git_project() as (project, _):
            self.make_conflict(project)
            file = conflict_file(project, "conflict", "hello.txt")
            resolve_conflict(project, "conflict", {"path": "hello.txt", "expectedHash": file["hash"], "choice": "ours"})
            original_save = integration_module._save
            def fail_once(project, session_id, state):
                if state.get("resultOid"):
                    raise OSError("simulated interrupted state write")
                return original_save(project, session_id, state)
            with patch.object(integration_module, "_save", fail_once):
                with self.assertRaises(OSError):
                    continue_integration(project, "conflict")
            self.assertEqual(continue_integration(project, "conflict")["status"], "completed")

    def test_conflict_file_path_and_stale_edits_fail_closed(self):
        with git_project() as (project, _):
            _, state = self.make_conflict(project)
            with self.assertRaises(GitWorkspaceError):
                conflict_file(project, "conflict", "../state.json")
            file = conflict_file(project, "conflict", "hello.txt")
            (Path(state["workspace"]) / "hello.txt").write_text("external edit\n")
            with self.assertRaises(GitWorkspaceError):
                resolve_conflict(project, "conflict", {"path": "hello.txt", "expectedHash": file["hash"], "choice": "manual", "content": "old editor\n"})
            self.assertEqual((Path(state["workspace"]) / "hello.txt").read_text(), "external edit\n")


class IntegrationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_creation_target_settings_commit_and_integration_api(self):
        with git_project() as (fixture, home):
            projects = ProjectStore(FileStorage(home / "state"))
            sessions = SessionStore(FileStorage(home / "state"), project_store=projects)
            limiter = RequestConcurrencyLimiter(3, 0.1)
            app = FastAPI()
            app.include_router(build_project_router(projects, sessions))
            app.include_router(build_integration_router(projects, sessions, limiter))
            project = await projects.create(ProjectCreateInput(rootPath=fixture.root_path))
            _git(Path(project.root_path), "branch", "release")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.patch(f"/api/projects/{project.id}", json={"targetBranch": "release"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["targetBranch"], "release")
                restarted = ProjectStore(FileStorage(home / "state"))
                self.assertEqual((await restarted.get(project.id)).target_branch, "release")
                bad = await client.patch(f"/api/projects/{project.id}", json={"targetBranch": "does-not-exist"})
                self.assertEqual(bad.status_code, 400)
                first = (await client.post(f"/api/projects/{project.id}/sessions")).json()
                second = (await client.post(f"/api/projects/{project.id}/sessions")).json()
                self.assertEqual(first["environment"]["kind"], "local")
                self.assertEqual(first["environment"]["cwd"], second["environment"]["cwd"])
                isolated = (await client.post(f"/api/projects/{project.id}/sessions", json={"environment": "worktree"})).json()
                self.assertEqual(isolated["environment"]["kind"], "worktree")
                (Path(isolated["environment"]["cwd"]) / "api.txt").write_text("from API\n")
                prefix = f"/api/sessions/{isolated['id']}"
                response = await client.post(prefix + "/integration")
                self.assertEqual(response.status_code, 409)
                async with limiter.protect("other-session", workspace_key=str(Path(project.root_path).resolve())):
                    response = await client.post(prefix + "/git/commit", json={"message": "must wait for target"})
                    self.assertEqual(response.status_code, 409)
                response = await client.post(prefix + "/git/commit", json={"message": "API task"})
                self.assertEqual(response.status_code, 200, response.text)
                response = await client.post(prefix + "/integration")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "completed")
                self.assertEqual((await client.get(prefix + "/integration")).json()["status"], "completed")
