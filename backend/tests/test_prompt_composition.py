from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.mcp_tool import McpToolDescriptor
from backend.memory import MemoryFile, MemoryType, trusted_tool_paths
from backend.prompts import (
    McpInstruction,
    PersonaInputs,
    PromptInputs,
    compose_prompt,
)
from backend.tools import SkillCatalog, build_tool_catalog
from backend.tools.local import ToolDefinition, build_skill_tool


async def _noop(_: dict) -> str:
    return "ok"


class PromptCompositionTests(unittest.TestCase):
    def test_compiler_preserves_sealed_contract_and_channel_boundaries(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = MemoryFile(
                path=root / "managed.md",
                content="managed policy",
                type=MemoryType.MANAGED,
            )
            project = MemoryFile(
                path=root / "CLAUDE.md",
                content="project instruction",
                type=MemoryType.PROJECT,
            )
            auto = MemoryFile(
                path=root / "MEMORY.md",
                content="background preference",
                type=MemoryType.AUTOMATED,
            )
            local_tools = [
                ToolDefinition("AskUserQuestion", "ask", {}, _noop),
                ToolDefinition("Skill", "skills", {}, _noop),
            ]
            mcp_tools = [McpToolDescriptor("calendar", "create_event", "create", {})]
            catalog = build_tool_catalog(local_tools=local_tools, mcp_tools=mcp_tools)

            bundle = compose_prompt(
                PromptInputs(
                    instruction_root=root,
                    output_workspace=root / "session-output",
                    memory_files=(managed, project, auto),
                    tool_catalog=catalog,
                    mcp_instructions=(
                        McpInstruction("calendar", "external server guidance"),
                    ),
                    persona=PersonaInputs(custom="base persona", override="custom persona"),
                    permission_mode="default",
                    options={"voiceConversation": True},
                    team_id=None,
                )
            )

            self.assertEqual(bundle.system_prompt.count("You are K Agent"), 1)
            self.assertIn("Runtime permission checks", bundle.system_prompt)
            self.assertIn("custom persona", bundle.system_prompt)
            self.assertNotIn("base persona", bundle.system_prompt)
            self.assertIn("managed policy", bundle.system_prompt)
            project_sections = [
                item for item in bundle.sections if item.content == "project instruction"
            ]
            self.assertEqual([item.channel for item in project_sections], ["context"])
            self.assertNotIn("mcp__calendar__create_event", bundle.system_prompt)
            self.assertIn("project instruction", bundle.context_message or "")
            self.assertIn("background preference", bundle.context_message or "")
            self.assertIn("external server guidance", bundle.context_message or "")
            self.assertEqual(
                set(bundle.initial_memory_paths),
                {str(managed.path.resolve()), str(project.path.resolve()), str(auto.path.resolve())},
            )

    def test_skill_catalog_owns_request_scoped_skill_description(self) -> None:
        skills = SkillCatalog.from_skills(
            [
                {"id": "writer", "name": "writer", "description": "Draft reports", "enabled": True},
                {"id": "hidden", "description": "No", "enabled": False},
            ]
        )

        tool = build_skill_tool(skill_catalog=skills)

        self.assertIn("writer", tool.description)
        self.assertNotIn("hidden", tool.description)
        self.assertNotIn("enum", tool.parameters["properties"]["skill"])

    def test_relative_session_paths_cannot_trigger_project_rules(self) -> None:
        with TemporaryDirectory() as project_tmp, TemporaryDirectory() as session_tmp:
            project = Path(project_tmp).resolve()
            session = Path(session_tmp).resolve()
            project_file = project / "backend" / "main.py"

            self.assertEqual(
                trusted_tool_paths(
                    "Read",
                    {"file_path": "backend/main.py"},
                    instruction_root=project,
                    tool_workspace=session,
                ),
                [],
            )
            self.assertEqual(
                trusted_tool_paths(
                    "Read",
                    {"file_path": str(project_file)},
                    instruction_root=project,
                    tool_workspace=session,
                ),
                [project_file],
            )
            # Text returned by tools is not an accepted input to this API.
            self.assertEqual(
                trusted_tool_paths(
                    "WebFetch",
                    {"path": str(project_file)},
                    instruction_root=project,
                    tool_workspace=session,
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
