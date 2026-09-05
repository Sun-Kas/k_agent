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
from backend.prompts.skills import MAX_LISTING_DESC_CHARS
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
            skills = SkillCatalog.from_skills(
                [
                    {
                        "id": "writer",
                        "name": "writer",
                        "description": "Draft reports",
                        "instructions": "FULL SKILL BODY MUST STAY LAZY",
                        "enabled": True,
                    },
                    {
                        "id": "hidden",
                        "description": "Must not be visible",
                        "enabled": False,
                    },
                ]
            )

            bundle = compose_prompt(
                PromptInputs(
                    instruction_root=root,
                    output_workspace=root / "session-output",
                    memory_files=(managed, project, auto),
                    tool_catalog=catalog,
                    skill_catalog=skills,
                    context_window_tokens=128_000,
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
            self.assertIn("available_skills", bundle.context_message or "")
            self.assertIn("- writer: Draft reports", bundle.context_message or "")
            self.assertNotIn("hidden", bundle.context_message or "")
            self.assertNotIn("FULL SKILL BODY", bundle.context_message or "")
            self.assertNotIn("writer", bundle.system_prompt)
            self.assertEqual(bundle.skill_listing_count, 1)
            self.assertEqual(bundle.skill_listing_truncated_count, 0)
            self.assertEqual(
                set(bundle.initial_memory_paths),
                {str(managed.path.resolve()), str(project.path.resolve()), str(auto.path.resolve())},
            )

    def test_skill_tool_description_is_stable_across_request_catalogs(self) -> None:
        writer_skills = SkillCatalog.from_skills(
            [
                {"id": "writer", "name": "writer", "description": "Draft reports", "enabled": True},
            ]
        )
        reviewer_skills = SkillCatalog.from_skills(
            [
                {"id": "reviewer", "name": "reviewer", "description": "Review reports", "enabled": True},
            ]
        )

        writer_tool = build_skill_tool(skill_catalog=writer_skills)
        reviewer_tool = build_skill_tool(skill_catalog=reviewer_skills)

        self.assertEqual(writer_tool.description, reviewer_tool.description)
        self.assertNotIn("writer", writer_tool.description)
        self.assertNotIn("reviewer", reviewer_tool.description)
        self.assertIn("request context", writer_tool.description)
        self.assertNotIn("enum", writer_tool.parameters["properties"]["skill"])

    def test_skill_listing_applies_entry_and_total_budgets(self) -> None:
        root = Path("/tmp/project")
        local_tools = [ToolDefinition("Skill", "stable", {}, _noop)]
        tool_catalog = build_tool_catalog(local_tools=local_tools, mcp_tools=[])
        long_description = "界" * (MAX_LISTING_DESC_CHARS + 50)
        skill_catalog = SkillCatalog.from_skills(
            [
                {
                    "id": "writer",
                    "description": long_description,
                    "enabled": True,
                },
                {
                    "id": "reviewer",
                    "description": "Review reports",
                    "whenToUse": "Use for final reviews",
                    "enabled": True,
                },
            ]
        )

        roomy = compose_prompt(
            PromptInputs(
                instruction_root=root,
                output_workspace=None,
                memory_files=(),
                tool_catalog=tool_catalog,
                skill_catalog=skill_catalog,
                context_window_tokens=128_000,
            )
        )
        roomy_context = roomy.context_message or ""
        self.assertIn("- writer: " + "界" * (MAX_LISTING_DESC_CHARS - 1) + "…", roomy_context)
        self.assertIn("Review reports - Use for final reviews", roomy_context)
        self.assertEqual(roomy.skill_listing_truncated_count, 1)

        constrained = compose_prompt(
            PromptInputs(
                instruction_root=root,
                output_workspace=None,
                memory_files=(),
                tool_catalog=tool_catalog,
                skill_catalog=skill_catalog,
                context_window_tokens=200,
            )
        )
        constrained_context = constrained.context_message or ""
        self.assertIn("- writer", constrained_context)
        self.assertIn("- reviewer", constrained_context)
        self.assertNotIn("Review reports", constrained_context)
        self.assertEqual(constrained.skill_listing_truncated_count, 2)

    def test_skill_listing_requires_provider_visible_skill_tool(self) -> None:
        root = Path("/tmp/project")
        skill_catalog = SkillCatalog.from_skills(
            [{"id": "writer", "description": "Draft reports", "enabled": True}]
        )
        tool_catalog = build_tool_catalog(local_tools=[], mcp_tools=[])

        bundle = compose_prompt(
            PromptInputs(
                instruction_root=root,
                output_workspace=None,
                memory_files=(),
                tool_catalog=tool_catalog,
                skill_catalog=skill_catalog,
                context_window_tokens=128_000,
            )
        )

        self.assertNotIn("available_skills", bundle.context_message or "")
        self.assertNotIn("writer", bundle.context_message or "")
        self.assertEqual(bundle.skill_listing_count, 0)

    def test_skill_listing_uses_when_to_use_not_instruction_body(self) -> None:
        root = Path("/tmp/project")
        tool_catalog = build_tool_catalog(
            local_tools=[ToolDefinition("Skill", "stable", {}, _noop)],
            mcp_tools=[],
        )
        skill_catalog = SkillCatalog.from_skills(
            [
                {
                    "id": "writer",
                    "description": "",
                    "whenToUse": "Draft the requested report.",
                    "instructions": "\n\nFULL BODY MUST NOT APPEAR\n",
                    "enabled": True,
                }
            ]
        )

        bundle = compose_prompt(
            PromptInputs(
                instruction_root=root,
                output_workspace=None,
                memory_files=(),
                tool_catalog=tool_catalog,
                skill_catalog=skill_catalog,
                context_window_tokens=None,
            )
        )

        self.assertIn("- writer: Draft the requested report.", bundle.context_message or "")
        self.assertNotIn("FULL BODY", bundle.context_message or "")

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
