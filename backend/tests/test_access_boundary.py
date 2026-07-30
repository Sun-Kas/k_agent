from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import Mock

from access_layer.gateway import AgentAccessLayer
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

    def test_access_layer_builds_complete_multimodal_messages(self) -> None:
        from backend.api.schemas import ChatMessage

        messages = [
            ChatMessage(
                id="user-1",
                role="user",
                content="describe",
                createdAt="2026-07-28T00:00:00+00:00",
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
            result[2]["content"][1]["image_url"]["url"],
            "data:image/png;base64,abc",
        )


if __name__ == "__main__":
    unittest.main()
