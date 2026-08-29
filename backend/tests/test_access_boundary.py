from __future__ import annotations

import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from ag_ui.core import RunAgentInput
from access_layer.gateway import AgentAccessLayer
from access_layer.sessions.store import SessionRecord
from backend.api.schemas import ChatMessage
from backend.agent import AgentRunRequest
from backend.context import compose_api_messages


class AccessBoundaryTests(unittest.TestCase):
    def test_access_layer_is_a_top_level_package_and_backend_does_not_depend_on_it(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.assertTrue((project_root / "access_layer" / "main.py").is_file())
        self.assertTrue((project_root / "access_layer" / "gateway.py").is_file())
        self.assertTrue((project_root / "backend" / "main.py").is_file())
        self.assertFalse((project_root / "backend" / "access").exists())

        violations: list[str] = []
        for path in (project_root / "backend").rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                if any(name == "access_layer" or name.startswith("access_layer.") for name in modules):
                    violations.append(str(path.relative_to(project_root)))
        self.assertEqual(violations, [])

    def test_agent_request_contract_has_no_session_identifier(self) -> None:
        self.assertNotIn("session_id", AgentRunRequest.__dataclass_fields__)
        self.assertNotIn("thread_id", AgentRunRequest.__dataclass_fields__)

    def test_access_layer_does_not_own_context_planning_or_provider_messages(self) -> None:
        gateway_path = Path(__file__).resolve().parents[2] / "access_layer" / "gateway.py"
        source = gateway_path.read_text(encoding="utf-8")
        self.assertNotIn("build_context_plan", source)
        self.assertNotIn("compose_api_messages", source)
        self.assertNotIn('"apiMessages"', source)

    def test_access_layer_does_not_import_backend_or_shared_packages(self) -> None:
        access_root = Path(__file__).resolve().parents[2] / "access_layer"
        violations: list[str] = []
        for path in access_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                for module in modules:
                    if module == "backend" or module.startswith("backend."):
                        violations.append(f"{path.relative_to(access_root.parent)}: {module}")
                    if module == "shared" or module.startswith("shared."):
                        violations.append(f"{path.relative_to(access_root.parent)}: {module}")
        self.assertEqual(violations, [])

    def test_agent_backend_does_not_import_session_prompt_memory_or_skills(self) -> None:
        agent_dir = Path(__file__).resolve().parents[1] / "agent"
        forbidden = {
            "access_layer",
            "backend.memory",
            "backend.prompts",
            "backend.request_context",
            "backend.sessions",
            "backend.skills",
        }
        violations: list[str] = []
        for path in agent_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if any(
                        node.module == prefix or node.module.startswith(f"{prefix}.")
                        for prefix in forbidden
                    ):
                        violations.append(f"{path.name}: {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(
                            alias.name == prefix or alias.name.startswith(f"{prefix}.")
                            for prefix in forbidden
                        ):
                            violations.append(f"{path.name}: {alias.name}")
        self.assertEqual(violations, [])

    def test_access_layer_has_only_transport_dependencies(self) -> None:
        layer = AgentAccessLayer(
            session_store=Mock(),
            request_limiter=Mock(),
            agent_backend_client=Mock(),
            runtime_catalog=Mock(),
        )
        self.assertEqual(
            set(layer.__dict__),
            {
                "_session_store",
                "_request_limiter",
                "_agent_backend_client",
                "_runtime_catalog",
            },
        )

    def test_activity_snapshot_is_encoded_as_a_regular_sse_frame(self) -> None:
        approval_event = {
            "type": "ACTIVITY_SNAPSHOT",
            "messageId": "approval-1",
            "activityType": "approval",
            "content": {"id": "approval-1", "runId": "run-1", "status": "pending"},
        }

        frame = AgentAccessLayer._encode_sse(approval_event)

        self.assertTrue(frame.startswith("data: "))
        self.assertIn('"ACTIVITY_SNAPSHOT"', frame)
        self.assertTrue(frame.endswith("\n\n"))
        self.assertNotIn(": flush ", frame)

    def test_regular_sse_frame_has_no_padding(self) -> None:
        frame = AgentAccessLayer._encode_sse({
            "type": "TOOL_CALL_START",
            "toolCallId": "tool-1",
            "toolCallName": "Bash",
        })

        self.assertTrue(frame.startswith("data: "))
        self.assertNotIn(": flush ", frame)

    def test_agent_backend_run_receives_resolved_catalog_entries(self) -> None:
        backend_main = (
            Path(__file__).resolve().parents[1] / "main.py"
        ).read_text(encoding="utf-8")
        self.assertIn('alias="mcpServers"', backend_main)
        self.assertIn("skills: list[dict[str, Any]]", backend_main)
        self.assertNotIn('alias="mcpServerIds"', backend_main)
        self.assertNotIn('alias="skillIds"', backend_main)
        self.assertNotIn('"/internal/config/skills"', backend_main)
        self.assertNotIn('"/internal/config/mcp"', backend_main)

    def test_agent_backend_runtime_does_not_derive_session_storage_paths(self) -> None:
        """Conversation storage is an Access Layer concern, including workspace lookup."""

        backend_root = Path(__file__).resolve().parents[1]
        paths = [backend_root / "main.py", *(backend_root / "runners").glob("*.py")]
        forbidden = {"session_bundle_dir", "session_json_path", "session_workspace_dir"}
        violations: list[str] = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in forbidden:
                            violations.append(f"{path.name}: {alias.name}")
                elif isinstance(node, ast.Name) and node.id in forbidden:
                    violations.append(f"{path.name}: {node.id}")
        self.assertEqual(violations, [])

    def test_access_layer_builds_complete_multimodal_messages(self) -> None:
        from backend.api.schemas import ChatMessage, MessageAttachment

        messages = [
            ChatMessage(
                id="user-1",
                role="user",
                content="describe",
                createdAt="2026-07-28T00:00:00+00:00",
                attachments=[
                    MessageAttachment(name="clip.mp4", type="video/mp4", dataUrl="data:video/mp4;base64,xyz")
                ],
            )
        ]
        result = compose_api_messages(
            messages,
            system_prompt="system",
            user_context={"memory": "remember this"},
            attachments=[{"dataUrl": "data:image/png;base64,abc"}],
        )

        self.assertEqual(result[0], {"role": "system", "content": "system"})
        self.assertIn("<system-reminder>", result[1]["content"])
        self.assertEqual(result[2]["content"][0]["text"], "describe")
        self.assertEqual(
            result[2]["content"][1]["video_url"]["url"],
            "data:video/mp4;base64,xyz",
        )

    def test_access_layer_validates_inline_media(self) -> None:
        attachments = AgentAccessLayer._attachments([
            {"name": "photo.png", "type": "image/png", "dataUrl": "data:image/png;base64,abc"}
        ])
        self.assertEqual(attachments[0].name, "photo.png")
        self.assertEqual(attachments[0].data_url, "data:image/png;base64,abc")

        with self.assertRaisesRegex(Exception, "Only image and video"):
            AgentAccessLayer._attachments([
                {"name": "notes.txt", "type": "text/plain", "dataUrl": "data:text/plain;base64,abc"}
            ])


class FullHistoryForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_layer_forwards_complete_history_and_explicit_workspace(self) -> None:
        now = datetime.now(timezone.utc)
        session = SessionRecord(
            id="session-history",
            title="History",
            messages=[
                ChatMessage(id="old-user", role="user", content="first", createdAt=now),
                ChatMessage(id="old-assistant", role="assistant", content="second", createdAt=now),
            ],
        )

        class Store:
            async def get_or_create(self, _session_id):
                return session

            async def ensure_accepts_new_input(self, _session_id):
                return None

            async def save_run_start(self, _session_id, messages, **_kwargs):
                session.messages.extend(messages)
                return session

            async def append_event(self, _session_id, _event):
                return None

        class Guard:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_args):
                return None

        class Limiter:
            def protect(self, _session_id):
                return Guard()

        class Catalog:
            def selected_runtime(self, _mcp_ids, _skill_ids):
                return [], []

        class Backend:
            def __init__(self):
                self.payload = None

            async def stream(self, payload, _request_id):
                self.payload = payload
                if False:
                    yield {}

        backend = Backend()
        layer = AgentAccessLayer(
            session_store=Store(),
            request_limiter=Limiter(),
            agent_backend_client=backend,
            runtime_catalog=Catalog(),
        )
        payload = RunAgentInput.model_validate({
            "threadId": session.id,
            "runId": "run-history",
            "state": {},
            "messages": [{"id": "new-user", "role": "user", "content": "third"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        })

        with unittest.mock.patch(
            "access_layer.gateway.session_workspace_dir",
            return_value=Path("/managed/state/sessions/session-history/workspace"),
        ), unittest.mock.patch(
            "access_layer.gateway.to_managed_path",
            return_value="state/sessions/session-history/workspace",
        ):
            response = await layer.run(payload)
            async for _chunk in response.body_iterator:
                pass

        assert backend.payload is not None
        self.assertEqual(
            [message["content"] for message in backend.payload["messages"]],
            ["first", "second", "third"],
        )
        self.assertEqual(
            backend.payload["workspaceDir"],
            "state/sessions/session-history/workspace",
        )


if __name__ == "__main__":
    unittest.main()
