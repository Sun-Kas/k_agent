"""把 system / user / memory / Skill / MCP 上下文拼成生效 prompt。

pipeline：`KAgentRunner` 每轮调用 `build_prompt_bundle`；静态 section 可指纹缓存，
动态 MCP/记忆变更走 `lifecycle.reset_prompt_caches`。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.memory import get_memory_context, get_memory_files, get_nested_memory_files, is_memory_file
from backend.prompts.mcp_prompt import McpPromptTool, build_mcp_dynamic_prompt
from backend.prompts.sections import PromptSection, fingerprint_text, render_sections, SECTION_CACHE


MEMORY_BEHAVIOR_PROMPT = """
Memory files may contain user and project instructions. Treat them as durable guidance for this workspace.
When memory instructions conflict with generic behavior, follow the memory instructions unless they are unsafe or impossible.
Do not mention memory files unless they are relevant to the user's request.
""".strip()


@dataclass(frozen=True)
class EffectivePrompt:
    """渲染后的 prompt，外加已加载上下文的可追溯元数据。"""

    system_prompt: str
    user_context: dict[str, str]
    system_context: dict[str, str]
    memory_paths: list[str]


def build_effective_system_prompt(
    base_prompt: str,
    *,
    skills: list[dict] | None = None,
    override_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    custom_system_prompt: str | None = None,
    agent_system_prompt: str | None = None,
    proactive: bool = False,
    mcp_tools: list[McpPromptTool] | None = None,
) -> str:
    """按 K Agent 优先级规则合成 system prompt（override 完全替换）。"""
    # override 是完全替换：连 Skill 摘要和 append 都不再拼接，
    # 用于调用方需要精确控制整段提示词的场景。
    if override_system_prompt:
        return override_system_prompt.strip()

    prompt = _default_system_prompt(base_prompt, mcp_tools=mcp_tools)
    if agent_system_prompt:
        agent_prompt = agent_system_prompt.strip()
        # proactive 决定子 agent 的指令是叠加在默认提示词之上，还是整体取代它。
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
    skills: list[dict] | None = None,
    custom_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    override_system_prompt: str | None = None,
    agent_system_prompt: str | None = None,
    proactive: bool = False,
    referenced_paths: list[Path] | None = None,
    mcp_tools: list[McpPromptTool] | None = None,
) -> EffectivePrompt:
    """单次模型请求用的完整 prompt 载荷（system + user/system context）。"""
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
    system_context = get_system_context()
    user_context, memory_paths = get_user_context(cwd, referenced_paths=referenced_paths)
    return EffectivePrompt(
        system_prompt=append_system_context(system_prompt, system_context),
        user_context=user_context,
        system_context=system_context,
        memory_paths=memory_paths,
    )


def get_system_context() -> dict[str, str]:
    """读取需要随请求变化的系统上下文片段。"""
    context: dict[str, str] = {}
    cache_breaker = os.getenv("K_AGENT_SYSTEM_PROMPT_INJECTION") or os.getenv("CLAUDE_CODE_SYSTEM_PROMPT_INJECTION")
    if cache_breaker:
        context["cacheBreaker"] = cache_breaker
    return context


def get_user_context(cwd: Path, *, referenced_paths: list[Path] | None = None) -> tuple[dict[str, str], list[str]]:
    """读取 memory 和日期等用户上下文。"""
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
    """把系统上下文追加进 system prompt。"""
    context_block = "\n".join(f"{key}: {value}" for key, value in context.items() if value)
    if not context_block:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{context_block}"


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
    # 只看最近 3 条消息：更早提到的路径与当前任务的相关性迅速下降，
    # 全量扫描会把大量陈旧路径的 memory 拉进上下文。
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
    """把路径候选分成文件路径和目录路径。"""
    memory_paths = []
    regular_paths = []
    for path in paths:
        if is_memory_file(path):
            memory_paths.append(path)
        else:
            regular_paths.append(path)
    return memory_paths, regular_paths


def _default_system_prompt(base_prompt: str, *, mcp_tools: list[McpPromptTool] | None = None) -> str:
    """生成包含默认行为和工具说明的系统提示词。"""
    # This compatibility helper predates typed sections and expects a complete
    # base prompt. Production composition owns identity separately.
    if "You are K Agent" not in base_prompt:
        base_prompt = f"You are K Agent, a helpful personal assistant.\n\n{base_prompt}"
    # 静态段按内容指纹缓存，动态段每次重建：MCP 工具清单会随连接状态变化，
    # 缓存它会让模型看到已经断开的工具。指纹保证 base_prompt 改动后自动失效。
    # 动态段排在最后，前面的稳定前缀才能被 provider 端前缀缓存命中。
    static_fingerprint = fingerprint_text(base_prompt)
    static_prompt = SECTION_CACHE.get(
        "default_system_static",
        static_fingerprint,
        lambda: render_sections([
            PromptSection("base", base_prompt.strip()),
            PromptSection("memory_behavior", MEMORY_BEHAVIOR_PROMPT),
            PromptSection(
                "style",
                "Use concise, direct language. Prefer concrete actions and verified facts.",
            ),
        ]),
    )
    dynamic_prompt = render_sections([
        PromptSection("mcp_tools", build_mcp_dynamic_prompt(mcp_tools or [])),
    ])
    return "\n\n".join(section for section in (static_prompt, dynamic_prompt) if section)


def _append_skills(base_prompt: str, skills: list[dict]) -> str:
    """把可用 Skill 摘要追加到系统提示词。"""
    enabled = [skill for skill in skills if _skill_enabled(skill)]
    if not enabled:
        return base_prompt
    blocks = "\n".join(_skill_summary(skill) for skill in enabled)
    # 这里只放摘要不放正文：Skill 正文可能很长，全量注入会挤占上下文预算。
    # 模型先看摘要判断相关性，真正需要时才通过 Skill 工具取回完整指令。
    return (
        f"{base_prompt.rstrip()}\n\n"
        "# Available Skills\n\n"
        "Use the single `Skill` tool to load a matching skill. Pass the selected "
        "name as `skill` and the user's task arguments as `args`. Never use a "
        "skill name as the tool/function name itself.\n\n"
        f"{blocks}"
    )


def _skill_enabled(skill: dict) -> bool:
    """判断 Skill 是否允许模型主动调用。"""
    return bool(skill.get("enabled", True)) and bool(str(skill.get("description") or "").strip() or skill.get("name") or skill.get("id"))


def _skill_summary(skill: dict) -> str:
    """生成单个 Skill 的系统提示词摘要。"""
    name = skill.get("name") or skill.get("id")
    description = str(skill.get("description") or "").strip()
    when_to_use = str(skill.get("whenToUse") or skill.get("when_to_use") or "").strip()
    if when_to_use and when_to_use not in description:
        description = f"{description} - {when_to_use}" if description else when_to_use
    return f"- {name}: {description}" if description else f"- {name}"


def _dedupe_memory_files(memory_files: list) -> list:
    """按路径去重 memory 文件。"""
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
    """按常见字符串规则解析布尔值。"""
    return value is not None and value.lower() not in {"", "0", "false", "no", "off"}


def json_like_text(value: object) -> str:
    """把结构化值转成便于路径提取的文本。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def re_path_candidates(text: str) -> list[str]:
    """从文本中提取可能的文件路径。"""
    import re

    pattern = r"(?<![\\w.-])(?:/[^\\s'\"`<>]+|\\.\\.?/[^\\s'\"`<>]+)"
    return [item.strip(".,;:，。)]}") for item in re.findall(pattern, text)]
