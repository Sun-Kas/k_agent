from __future__ import annotations

import os
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.memory import get_memory_context, get_memory_files, get_nested_memory_files, is_memory_file
from backend.prompts.mcp_prompt import McpPromptTool, build_mcp_dynamic_prompt
from backend.prompts.sections import PromptSection, fingerprint_text, render_sections, SECTION_CACHE

if TYPE_CHECKING:
    from backend.skills.loader import SkillDefinition


# The prompt pipeline keeps long-lived behavior separate from per-request context:
# system prompt = durable behavior and runtime state;
# user context = memory/date wrapped in a <system-reminder> before the transcript.
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


CLI_SYSTEM_PREFIX = """
You are K Agent, an interactive personal assistant.
Help the user think, plan, answer questions, and complete tasks across their workspace.
Use available tools when they improve accuracy or let you take concrete action.
""".strip()


MEMORY_BEHAVIOR_PROMPT = """
Memory files may contain user and project instructions. Treat them as durable guidance for this workspace.
When memory instructions conflict with generic behavior, follow the memory instructions unless they are unsafe or impossible.
Do not mention memory files unless they are relevant to the user's request.
""".strip()


@dataclass(frozen=True)
class EffectivePrompt:
    system_prompt: str
    user_context: dict[str, str]
    system_context: dict[str, str]
    memory_paths: list[str]


def build_effective_system_prompt(
    base_prompt: str,
    *,
    skills: list[dict | "SkillDefinition"] | None = None,
    override_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    custom_system_prompt: str | None = None,
    agent_system_prompt: str | None = None,
    proactive: bool = False,
    mcp_tools: list[McpPromptTool] | None = None,
) -> str:
    """Apply K Agent's precedence rules for system prompts."""
    if override_system_prompt:
        return override_system_prompt.strip()

    prompt = _default_system_prompt(base_prompt, mcp_tools=mcp_tools)
    if agent_system_prompt:
        agent_prompt = agent_system_prompt.strip()
        if proactive:
            prompt = f"{prompt}\n\n# Custom Agent Instructions\n\n{agent_prompt}"
        else:
            prompt = agent_prompt
    elif custom_system_prompt:
        prompt = custom_system_prompt.strip()

    prompt = _append_skills(prompt, skills or [])
    if append_system_prompt:
        prompt = f"{prompt.rstrip()}\n\n{append_system_prompt.strip()}"
    return prompt.strip()


def build_prompt_bundle(
    base_prompt: str,
    *,
    cwd: Path | None = None,
    skills: list[dict | "SkillDefinition"] | None = None,
    custom_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    override_system_prompt: str | None = None,
    agent_system_prompt: str | None = None,
    proactive: bool = False,
    referenced_paths: list[Path] | None = None,
    mcp_tools: list[McpPromptTool] | None = None,
) -> EffectivePrompt:
    """Build the complete prompt payload used for a single model request."""
    cwd = (cwd or Path.cwd()).resolve()
    system_prompt = build_effective_system_prompt(
        base_prompt,
        skills=skills,
        custom_system_prompt=custom_system_prompt,
        append_system_prompt=append_system_prompt,
        override_system_prompt=override_system_prompt,
        agent_system_prompt=agent_system_prompt,
        proactive=proactive,
        mcp_tools=mcp_tools,
    )
    system_context = get_system_context(cwd)
    user_context, memory_paths = get_user_context(cwd, referenced_paths=referenced_paths)
    return EffectivePrompt(
        system_prompt=append_system_context(system_prompt, system_context),
        user_context=user_context,
        system_context=system_context,
        memory_paths=memory_paths,
    )


def get_system_context(cwd: Path) -> dict[str, str]:
    context: dict[str, str] = {}
    if not _truthy(os.getenv("K_AGENT_DISABLE_GIT_CONTEXT") or os.getenv("CLAUDE_CODE_DISABLE_GIT_CONTEXT")):
        git_status = _git_status(cwd)
        if git_status:
            context["gitStatus"] = git_status
    cache_breaker = os.getenv("K_AGENT_SYSTEM_PROMPT_INJECTION") or os.getenv("CLAUDE_CODE_SYSTEM_PROMPT_INJECTION")
    if cache_breaker:
        context["cacheBreaker"] = cache_breaker
    return context


def get_user_context(cwd: Path, *, referenced_paths: list[Path] | None = None) -> tuple[dict[str, str], list[str]]:
    context: dict[str, str] = {
        "currentDate": f"Today's date is {datetime.now().date().isoformat()}."
    }
    if _truthy(os.getenv("K_AGENT_DISABLE_MEMORY") or os.getenv("CLAUDE_CODE_DISABLE_CLAUDE_MDS")):
        return context, []

    memory_files = get_memory_files(cwd)
    for path in referenced_paths or []:
        memory_files.extend(get_nested_memory_files(path, cwd))
    deduped_memory = _dedupe_memory_files(memory_files)
    memory = get_memory_context(deduped_memory)
    if memory:
        context["memory"] = memory
    return context, [str(memory_file.path) for memory_file in deduped_memory]


def append_system_context(system_prompt: str, context: dict[str, str]) -> str:
    context_block = "\n".join(f"{key}: {value}" for key, value in context.items() if value)
    if not context_block:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{context_block}"


def prepend_user_context(messages: list[dict], context: dict[str, str]) -> list[dict]:
    """Prepend memory/date context as a user message without mutating history."""
    if not context:
        return messages
    body = "\n".join(f"# {key}\n{value}" for key, value in context.items() if value)
    if not body:
        return messages
    reminder = (
        "<system-reminder>\n"
        "As you answer the user's questions, you can use the following context:\n"
        f"{body}\n\n"
        "IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context "
        "unless it is highly relevant to the user's request.\n"
        "</system-reminder>"
    )
    return [{"role": "user", "content": reminder}, *messages]


def build_nested_memory_context(cwd: Path, paths: list[Path], loaded_paths: set[str]) -> tuple[dict[str, str], list[str]]:
    """Build a one-off context block for memory discovered after tool use."""
    memory_files = []
    for path in paths:
        memory_files.extend(get_nested_memory_files(path, cwd))
    fresh_files = [item for item in _dedupe_memory_files(memory_files) if str(item.path) not in loaded_paths]
    if not fresh_files:
        return {}, []
    memory = get_memory_context(fresh_files)
    loaded = [str(item.path) for item in fresh_files]
    if not memory:
        return {}, loaded
    return {"nestedMemory": memory}, loaded


def extract_referenced_paths(messages: list) -> list[Path]:
    """Best-effort path extraction for loading nested memory near mentioned files."""
    paths: list[Path] = []
    for message in messages[-3:]:
        content = getattr(message, "content", "") or ""
        for token in content.replace("\n", " ").split():
            cleaned = token.strip("\"'`()[]{}，。,:;")
            if cleaned.startswith("/") or cleaned.startswith("./") or cleaned.startswith("../"):
                paths.append(Path(cleaned))
    return paths


def extract_paths_from_value(value: object) -> list[Path]:
    """Best-effort path extraction from tool arguments or textual tool output."""
    text = json_like_text(value)
    paths: list[Path] = []
    for match in re_path_candidates(text):
        paths.append(Path(match))
    return paths


def classify_paths_for_memory(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    memory_paths = []
    regular_paths = []
    for path in paths:
        if is_memory_file(path):
            memory_paths.append(path)
        else:
            regular_paths.append(path)
    return memory_paths, regular_paths


def _default_system_prompt(base_prompt: str, *, mcp_tools: list[McpPromptTool] | None = None) -> str:
    static_fingerprint = fingerprint_text(base_prompt)
    static_prompt = SECTION_CACHE.get(
        "default_system_static",
        static_fingerprint,
        lambda: render_sections([
            PromptSection("identity", CLI_SYSTEM_PREFIX),
            PromptSection("base", base_prompt.strip()),
        ]),
    )
    dynamic_prompt = render_sections([
        PromptSection("memory_behavior", MEMORY_BEHAVIOR_PROMPT, cacheable=False),
        PromptSection("mcp_tools", build_mcp_dynamic_prompt(mcp_tools or []), cacheable=False),
        PromptSection("style", "Use concise, direct language. Prefer concrete actions and verified facts.", cacheable=False),
    ])
    sections = [
        static_prompt,
        SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
        dynamic_prompt,
    ]
    return "\n\n".join(section for section in sections if section)


def _append_skills(base_prompt: str, skills: list[dict | "SkillDefinition"]) -> str:
    enabled = [skill for skill in skills if _skill_enabled(skill)]
    if not enabled:
        return base_prompt
    blocks = "\n".join(_skill_summary(skill) for skill in enabled)
    return (
        f"{base_prompt.rstrip()}\n\n"
        "# Available Skills\n\n"
        "Use the Skill tool to load a skill's full instructions only when its description or when_to_use matches the task.\n\n"
        f"{blocks}"
    )


def _skill_enabled(skill: dict | "SkillDefinition") -> bool:
    if not isinstance(skill, dict):
        return not bool(getattr(skill, "disable_model_invocation", False))
    return bool(skill.get("enabled", True)) and bool(skill.get("instructions", skill.get("description", "")).strip())


def _skill_summary(skill: dict | "SkillDefinition") -> str:
    if not isinstance(skill, dict):
        parts = [f"- {getattr(skill, 'name')}: {getattr(skill, 'description')}"]
        if getattr(skill, "when_to_use", None):
            parts.append(f"  when_to_use: {getattr(skill, 'when_to_use')}")
        if getattr(skill, "argument_hint", None):
            parts.append(f"  args: {getattr(skill, 'argument_hint')}")
        if getattr(skill, "paths", None):
            parts.append(f"  paths: {', '.join(getattr(skill, 'paths'))}")
        return "\n".join(parts)
    name = skill.get("name") or skill.get("id")
    description = skill.get("description") or skill.get("instructions", "").strip().splitlines()[0]
    return f"- {name}: {description}"


def _git_status(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _dedupe_memory_files(memory_files: list) -> list:
    seen: set[Path] = set()
    deduped = []
    for memory_file in memory_files:
        path = memory_file.path.resolve()
        if path in seen:
            continue
        seen.add(path)
        deduped.append(memory_file)
    return deduped


def _truthy(value: str | None) -> bool:
    return value is not None and value.lower() not in {"", "0", "false", "no", "off"}


def json_like_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def re_path_candidates(text: str) -> list[str]:
    import re

    pattern = r"(?<![\\w.-])(?:/[^\\s'\"`<>]+|\\.\\.?/[^\\s'\"`<>]+)"
    return [item.strip(".,;:，。)]}") for item in re.findall(pattern, text)]
