"""The sole K Agent system/context prompt composition entry point."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from backend.prompts import contract, identity, mcp, persona, runtime_policy, skills, style
from backend.prompts import tool_guidance, tool_protocol
from backend.prompts.context import render as render_context
from backend.prompts.memory import build as build_memory
from backend.prompts.models import PromptBundle, PromptInputs, PromptSection


def compose_prompt(inputs: PromptInputs) -> PromptBundle:
    """Compile a request snapshot in one fixed, reviewable order."""

    memory = build_memory(inputs)
    skill_listing = skills.build(inputs)
    system_sections = (
        *identity.build(inputs),
        *contract.build(inputs),
        *(item for item in memory.sections if item.channel == "system"),
        *persona.build(inputs),
        *tool_protocol.build(inputs),
        *tool_guidance.build(inputs),
        *style.build(inputs),
        *runtime_policy.build(inputs),
    )
    context_sections = (
        PromptSection(
            name="current_date",
            content=f"Today's date is {datetime.now().date().isoformat()}.",
            channel="context",
            authority="platform",
            volatility="turn",
            instruction_mode="context_only",
            source="backend.clock",
        ),
        # Path disambiguation only. File bodies are loaded earlier as
        # CLAUDE.md / CLAUDE.local.md / ~/.claude/CLAUDE.md / .claude/rules
        # and rendered in memory.sections below — not inlined here.
        PromptSection(
            name="instruction_root",
            content=(
                f"Project instructions were discovered relative to `{inputs.instruction_root}`. "
                "This path is not the output workspace and does not by itself grant write access."
            ),
            channel="context",
            authority="platform",
            volatility="request",
            instruction_mode="context_only",
            source="prompt_inputs.instruction_root",
            sensitive=True,
        ),
        *(item for item in memory.sections if item.channel == "context"),
        # Skill 摘要用于发现，不进入动态 tool description，也不扩大执行白名单。
        *skill_listing.sections,
        # Selected connections (catalog metadata). Not tool schemas.
        *_mcp_server_context(inputs),
        # Optional MCP handshake `instructions` (InitializeResult), if any.
        *mcp.build(inputs),
    )
    sections = tuple((*system_sections, *context_sections))
    return PromptBundle(
        system_prompt="\n\n".join(item.content for item in system_sections if item.content),
        context_message=render_context(tuple(context_sections)),
        sections=sections,
        initial_memory_paths=memory.loaded_paths,
        stable_fingerprint=_fingerprint(
            tuple(item for item in sections if item.volatility == "static")
        ),
        dynamic_fingerprint=_fingerprint(
            tuple(item for item in sections if item.volatility != "static")
        ),
        skill_listing_chars=skill_listing.listing_chars,
        skill_listing_count=skill_listing.included_count,
        skill_listing_truncated_count=skill_listing.truncated_count,
    )


def _mcp_server_context(inputs: PromptInputs) -> tuple[PromptSection, ...]:
    """Which MCP connections were selected this run — capability context only."""
    if not inputs.mcp_servers:
        return ()
    lines = []
    for server in inputs.mcp_servers:
        server_id = server.get("id")
        name = server.get("name") or server_id
        description = str(server.get("description") or "").strip()
        lines.append(f"- {name} ({server_id})" + (f": {description}" if description else ""))
    return (
        PromptSection(
            name="selected_mcp_servers",
            content=(
                "The following MCP connections were selected for this run. Selection is capability context, not authorization:\n"
                + "\n".join(lines)
            ),
            channel="context",
            authority="external",
            volatility="request",
            instruction_mode="context_only",
            source="runner_context.mcp_servers",
        ),
    )


def _fingerprint(sections: tuple[PromptSection, ...]) -> str:
    payload = [
        {
            "name": item.name,
            "content": item.content,
            "channel": item.channel,
            "authority": item.authority,
            "source": item.source,
        }
        for item in sections
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
