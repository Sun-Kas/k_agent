"""Phase 4：Team 创建时按 projectId 解析 workspaceDir（见技术方案 §10.4）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from access_layer.projects.models import ProjectCreateInput
from access_layer.projects.router import build_project_router
from access_layer.projects.store import ProjectStore
from access_layer.sessions.store import SessionStore
from access_layer.storage.file import FileStorage
from access_layer.teams.router import build_team_router
from access_layer.teams.runtime import TeamRuntime
from access_layer.teams.store import TeamStore


def _team_payload(**overrides) -> dict:
    payload = {
        "name": "绑定 Project 的团队",
        "goal": "验证 projectId -> workspaceDir",
        "mode": "manual",
        "agents": [
            {"name": "主管", "role": "Supervisor", "agentKind": "k_agent", "isSupervisor": True},
        ],
    }
    payload.update(overrides)
    return payload


class ProjectTeamBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        home = Path(self._tmp.name)
        storage = FileStorage(home / "state")
        self.project_store = ProjectStore(storage)
        self.session_store = SessionStore(storage, project_store=self.project_store)
        self.team_store = TeamStore(home / "teams" / "team_runtime.db")
        await self.team_store.initialize()
        self.team_runtime = TeamRuntime(
            store=self.team_store,
            backend_client=None,
            runtime_catalog=None,
            enabled=False,
        )

        app = FastAPI()

        class _RuntimeProxy:
            store = self.team_store

        app.include_router(build_project_router(self.project_store, self.session_store))
        app.include_router(build_team_router(_RuntimeProxy(), self.project_store))
        self.client = TestClient(app)
        self.project_dir = TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()
        self.project_dir.cleanup()

    async def test_team_workspace_dir_resolves_from_project_id(self) -> None:
        create_response = self.client.post(
            "/api/projects", json={"rootPath": self.project_dir.name}
        )
        self.assertEqual(create_response.status_code, 200)
        project = create_response.json()

        team_response = self.client.post(
            "/api/teams", json=_team_payload(projectId=project["id"])
        )
        self.assertEqual(team_response.status_code, 200, team_response.text)
        team = team_response.json()
        self.assertEqual(
            Path(team["workspaceDir"]).resolve(), Path(self.project_dir.name).resolve()
        )

    async def test_project_id_and_workspace_dir_are_mutually_exclusive(self) -> None:
        create_response = self.client.post(
            "/api/projects", json={"rootPath": self.project_dir.name}
        )
        project = create_response.json()

        team_response = self.client.post(
            "/api/teams",
            json=_team_payload(projectId=project["id"], workspaceDir="/tmp/explicit"),
        )
        self.assertEqual(team_response.status_code, 422)

    async def test_create_session_under_project_via_http(self) -> None:
        create_response = self.client.post(
            "/api/projects", json={"rootPath": self.project_dir.name}
        )
        project = create_response.json()

        session_response = self.client.post(f"/api/projects/{project['id']}/sessions")
        self.assertEqual(session_response.status_code, 200, session_response.text)
        session = session_response.json()
        self.assertEqual(session["projectId"], project["id"])

        listing_response = self.client.get(f"/api/projects/{project['id']}/sessions")
        self.assertEqual(listing_response.status_code, 200)
        listed_ids = {item["id"] for item in listing_response.json()}
        self.assertIn(session["id"], listed_ids)


if __name__ == "__main__":
    unittest.main()
