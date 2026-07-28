from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.memory import append_auto_memory, compact_auto_memory, read_auto_memory, search_auto_memory
from backend.skills import get_available_skills


ToolExecutor = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: ToolExecutor


async def get_current_time(_: dict[str, Any]) -> str:
    return json.dumps({"now": datetime.now(timezone.utc).isoformat()})


async def echo_text(payload: dict[str, Any]) -> str:
    return json.dumps({"echoed": str(payload.get("text", ""))})


async def read_personal_memory(_: dict[str, Any]) -> str:
    path, content = read_auto_memory()
    return json.dumps({"path": str(path), "content": content}, ensure_ascii=False)


async def append_personal_memory(payload: dict[str, Any]) -> str:
    text = str(payload.get("text", "")).strip()
    if not text:
        return json.dumps({"ok": False, "error": "text is required"}, ensure_ascii=False)
    path = append_auto_memory(text)
    return json.dumps({"ok": True, "path": str(path)}, ensure_ascii=False)


async def search_personal_memory(payload: dict[str, Any]) -> str:
    query = str(payload.get("query", "")).strip().lower()
    path, matches = search_auto_memory(query)
    return json.dumps({"path": str(path), "matches": matches}, ensure_ascii=False)


async def compact_personal_memory(payload: dict[str, Any]) -> str:
    max_items = int(payload.get("maxItems", 200))
    path, before, after = compact_auto_memory(max_items=max_items)
    return json.dumps({"ok": True, "path": str(path), "itemsBefore": before, "itemsAfter": after}, ensure_ascii=False)


async def invoke_skill(payload: dict[str, Any]) -> str:
    skill_name = str(payload.get("skill", "")).strip().lstrip("/")
    args = str(payload.get("args", "")).strip()
    if not skill_name:
        return json.dumps({"success": False, "error": "skill is required"}, ensure_ascii=False)
    skill = next((item for item in get_available_skills() if item.name == skill_name), None)
    if skill is None:
        return json.dumps({"success": False, "error": f"Unknown skill: {skill_name}"}, ensure_ascii=False)
    if skill.disable_model_invocation:
        return json.dumps({"success": False, "error": f"Skill {skill_name} cannot be invoked by the model"}, ensure_ascii=False)
    content = _render_skill_content(skill.content, args, skill.argument_names, skill.base_dir)
    hook_notes = _render_skill_hooks(skill.hooks)
    return json.dumps(
        {
            "success": True,
            "commandName": skill.name,
            "status": skill.execution_context,
            "allowedTools": list(skill.allowed_tools),
            "model": skill.model,
            "content": content,
            "hooks": hook_notes,
        },
        ensure_ascii=False,
    )


def build_skill_tool(mcp_prompt_caller: Callable[[str, str, dict[str, Any]], Awaitable[str]] | None = None) -> ToolDefinition:
    """Build Skill as a closure so MCP prompt skills can call the active manager."""

    async def execute(payload: dict[str, Any]) -> str:
        skill_name = str(payload.get("skill", "")).strip().lstrip("/")
        args = str(payload.get("args", "")).strip()
        if skill_name.startswith("mcp__") and mcp_prompt_caller is not None:
            _, server_id, *prompt_parts = skill_name.split("__")
            prompt_name = "__".join(prompt_parts)
            return await mcp_prompt_caller(server_id, prompt_name, {"args": args} if args else {})
        return await invoke_skill(payload)

    return ToolDefinition(
        name="Skill",
        description="Load and execute a named K Agent skill. Use this when an available skill matches the task.",
        parameters={
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The skill name, without a leading slash.",
                },
                "args": {
                    "type": "string",
                    "description": "Optional arguments for the skill.",
                },
            },
            "required": ["skill"],
            "additionalProperties": False,
        },
        execute=execute,
    )


def _render_skill_content(content: str, args: str, argument_names: tuple[str, ...], base_dir: str | None) -> str:
    rendered = content.replace("$ARGUMENTS", args)
    for index, name in enumerate(argument_names):
        value = args.split()[index] if index < len(args.split()) else ""
        rendered = rendered.replace(f"${{{name}}}", value)
    if base_dir:
        rendered = rendered.replace("${K_AGENT_SKILL_DIR}", base_dir).replace("${CLAUDE_SKILL_DIR}", base_dir)
    return rendered


def _render_skill_hooks(hooks: dict[str, Any]) -> list[str]:
    """Expose declarative skill hooks without executing arbitrary commands."""
    notes = []
    for name, value in hooks.items():
        notes.append(f"{name}: {value}")
    return notes


LEGACY_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_current_time",
        description="Get the current local server time in ISO format.",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        execute=get_current_time,
    ),
    ToolDefinition(
        name="echo_text",
        description="Echo user-provided text for testing the tool pipeline.",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to echo back",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        execute=echo_text,
    ),
    ToolDefinition(
        name="read_personal_memory",
        description="Read K Agent's project-local durable memory from data/memory/MEMORY.md.",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        execute=read_personal_memory,
    ),
    ToolDefinition(
        name="append_personal_memory",
        description="Append a concise durable memory item to this project's data/memory/MEMORY.md.",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "One concise project-local memory item to remember.",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        execute=append_personal_memory,
    ),
    ToolDefinition(
        name="search_personal_memory",
        description="Search this project's durable memory in data/memory/MEMORY.md.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        execute=search_personal_memory,
    ),
    ToolDefinition(
        name="compact_personal_memory",
        description="Deduplicate and trim this project's durable memory in data/memory/MEMORY.md.",
        parameters={
            "type": "object",
            "properties": {
                "maxItems": {
                    "type": "integer",
                    "description": "Maximum memory items to keep.",
                    "default": 200,
                }
            },
            "additionalProperties": False,
        },
        execute=compact_personal_memory,
    ),
]
