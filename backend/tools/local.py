"""内置本地工具：记忆读写、探针工具、以及按本轮定义执行的 Skill。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.memory import append_auto_memory, compact_auto_memory, read_auto_memory, search_auto_memory
from backend.skills import SkillBodyError, load_skill_body
from backend.tools.workspace import current_tool_workspace

if TYPE_CHECKING:
    from backend.tools.catalog import SkillCatalog
ToolExecutor = Callable[[dict[str, Any]], Awaitable[str]]


# Skill 目录属于 request context。这里保持逐请求不变的调用协议，避免选择变化导致
# Provider tools Schema 抖动并破坏 Prompt Cache。
SKILL_TOOL_DESCRIPTION = (
    "Load a K Agent skill or MCP prompt by exact name. Available K Agent skills "
    "are listed in the request context. Invoke a matching skill before continuing "
    "with the task; do not guess names."
)


@dataclass(slots=True)
class ToolDefinition:
    """描述一个本地工具的 schema 和执行函数。"""
    name: str
    description: str
    parameters: dict[str, Any]
    execute: ToolExecutor
    # 只影响 Provider Observation，不改变公开工具结果或真实执行语义。
    # 未声明（尤其 MCP）必须按 retain 处理，不能猜测可重放性。
    context_policy: dict[str, Any] | None = None


async def get_current_time(_: dict[str, Any]) -> str:
    """返回 UTC ISO 时间，供模型校准「现在」。"""
    return json.dumps({"now": datetime.now(timezone.utc).isoformat()})


async def echo_text(payload: dict[str, Any]) -> str:
    """返回输入文本用于工具链路测试。"""
    return json.dumps({"echoed": str(payload.get("text", ""))})


async def read_personal_memory(_: dict[str, Any]) -> str:
    """读取 `$K_AGENT_HOME/content/memory/MEMORY.md`。"""
    path, content = read_auto_memory()
    return json.dumps({"path": str(path), "content": content}, ensure_ascii=False)


async def append_personal_memory(payload: dict[str, Any]) -> str:
    """向 MEMORY.md 追加一条精简的持久记忆。"""
    text = str(payload.get("text", "")).strip()
    if not text:
        return json.dumps({"ok": False, "error": "text is required"}, ensure_ascii=False)
    path = append_auto_memory(text)
    return json.dumps({"ok": True, "path": str(path)}, ensure_ascii=False)


async def search_personal_memory(payload: dict[str, Any]) -> str:
    """在 MEMORY.md 中按子串搜索匹配行。"""
    query = str(payload.get("query", "")).strip().lower()
    path, matches = search_auto_memory(query)
    return json.dumps({"path": str(path), "matches": matches}, ensure_ascii=False)


async def compact_personal_memory(payload: dict[str, Any]) -> str:
    """去重并裁剪 MEMORY.md，控制持久记忆体积。"""
    max_items = int(payload.get("maxItems", 200))
    path, before, after = compact_auto_memory(max_items=max_items)
    return json.dumps({"ok": True, "path": str(path), "itemsBefore": before, "itemsAfter": after}, ensure_ascii=False)


async def invoke_skill(
    payload: dict[str, Any], skills: list[dict[str, Any]] | None = None
) -> str:
    """校验本轮 catalog 快照后，按 ID 懒加载 Skill 正文。"""
    # 容忍模型带上斜杠前缀（把 Skill 当斜杠命令写成 `/foo`）。
    skill_name = str(payload.get("skill", "")).strip().lstrip("/")
    args = str(payload.get("args", "")).strip()
    if not skill_name:
        return json.dumps({"success": False, "error": "skill is required"}, ensure_ascii=False)
    # 先用本次请求的 catalog 快照做授权。只有匹配成功后才允许碰磁盘；
    # 文件只贡献正文，参数、白名单和触发条件仍以请求 metadata 为准。
    skill = next(
        (
            item
            for item in skills or []
            if skill_name in {str(item.get("id")), str(item.get("name"))}
        ),
        None,
    )
    if skill is None:
        return json.dumps({"success": False, "error": f"Unknown skill: {skill_name}"}, ensure_ascii=False)
    if not skill.get("enabled", True) or skill.get("disableModelInvocation", False):
        return json.dumps({"success": False, "error": f"Skill {skill_name} cannot be invoked by the model"}, ensure_ascii=False)
    try:
        # 文件读取发生在工具已授权之后，并移出流式事件循环，避免较大的正文阻塞 SSE。
        body = await asyncio.to_thread(
            load_skill_body,
            str(skill.get("id") or ""),
        )
    except SkillBodyError as exc:
        return json.dumps(
            {
                "success": False,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    content = _render_skill_content(
        body.instructions,
        args,
        tuple(str(value) for value in skill.get("argumentNames", [])),
        str(body.base_dir),
    )
    hook_notes = _render_skill_hooks(skill.get("hooks", {}))
    return json.dumps(
        {
            "success": True,
            "commandName": skill.get("name") or skill.get("id"),
            "status": skill.get("executionContext", "inline"),
            "allowedTools": list(skill.get("allowedTools", [])),
            "model": skill.get("model"),
            "baseDir": str(body.base_dir),
            "filePath": str(body.file_path),
            "content": content,
            "hooks": hook_notes,
        },
        ensure_ascii=False,
    )


def build_skill_tool(
    mcp_prompt_caller: Callable[[str, str, dict[str, Any]], Awaitable[str]] | None = None,
    skills: list[dict[str, Any]] | None = None,
    *,
    skill_catalog: "SkillCatalog | None" = None,
) -> ToolDefinition:
    """以闭包绑定本轮 MCP prompt 调用与 skills，避免跨请求复用连接。"""

    async def execute(payload: dict[str, Any]) -> str:
        """Skill 入口：`mcp__` 前缀走 MCP prompt，否则走本地 Skill 定义。"""
        skill_name = str(payload.get("skill", "")).strip().lstrip("/")
        args = str(payload.get("args", "")).strip()
        # MCP server 暴露的 prompt 复用同一个 Skill 入口，靠 mcp__ 前缀区分，
        # 这样模型只需要认识一个工具，不必再学一套 prompt 调用协议。
        if skill_name.startswith("mcp__") and mcp_prompt_caller is not None:
            _, server_id, *prompt_parts = skill_name.split("__")
            prompt_name = "__".join(prompt_parts)
            return await mcp_prompt_caller(server_id, prompt_name, {"args": args} if args else {})
        bound_skills = list(skill_catalog.items) if skill_catalog is not None else skills
        return await invoke_skill(payload, bound_skills)

    skill_property: dict[str, Any] = {
        "type": "string",
        "description": "The skill or MCP prompt name, without a leading slash.",
    }
    # Do not add an enum: the same entry point also accepts dynamically listed
    # MCP prompt names, which are not part of the local SkillCatalog.

    return ToolDefinition(
        name="Skill",
        description=SKILL_TOOL_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "skill": skill_property,
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
    """渲染 Skill 的正文、路径和使用条件。"""
    rendered = content.replace("$ARGUMENTS", args)
    for index, name in enumerate(argument_names):
        value = args.split()[index] if index < len(args.split()) else ""
        rendered = rendered.replace(f"${{{name}}}", value)
    workspace = current_tool_workspace()
    workspace_path = str(workspace) if workspace is not None else None
    if base_dir:
        # Community skills use several spellings for the package root. Expand all
        # of them so the model never has to `find` the skill directory.
        for token in (
            "${K_AGENT_SKILL_DIR}",
            "${CLAUDE_SKILL_DIR}",
            "${SKILL_DIR}",
            "$SKILL_DIR",
            "{SKILL_DIR}",
        ):
            rendered = rendered.replace(token, base_dir)
        header_lines = [
            f"Skill package root: {base_dir}",
            f"SKILL.md directory: {base_dir}",
            "Resolve relative paths in this Skill (scripts/, references/, "
            "assets/, templates/) against the package root above.",
        ]
        if workspace_path:
            # Community skills often hardcode /tmp; redirect artifacts into the
            # session collaboration workspace that local tools can actually write.
            rendered = rendered.replace("/tmp/", f"{workspace_path}/")
            header_lines.extend(
                [
                    f"Session workspace (write outputs here): {workspace_path}",
                    "Rewrite any /tmp output paths to this workspace. "
                    "Do not write deliverables to the repository root.",
                ]
            )
        rendered = "\n".join(header_lines) + "\n\n" + rendered
    elif workspace_path:
        rendered = (
            f"Session workspace (write outputs here): {workspace_path}\n\n"
            + rendered.replace("/tmp/", f"{workspace_path}/")
        )
    return rendered


def _render_skill_hooks(hooks: dict[str, Any]) -> list[str]:
    """仅把声明式 hooks 渲染成文本说明，绝不执行（防任意代码执行）。"""
    # 刻意只把 hook 渲染成文本说明返回给模型，绝不在这里执行：
    # Skill 文件可由用户导入的 zip 提供，执行其中的命令等于任意代码执行。
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
        description="Read K Agent's durable memory from $K_AGENT_HOME/content/memory/MEMORY.md.",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        execute=read_personal_memory,
    ),
    ToolDefinition(
        name="append_personal_memory",
        description="Append a concise durable memory item to $K_AGENT_HOME/content/memory/MEMORY.md.",
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
        description="Search durable memory in $K_AGENT_HOME/content/memory/MEMORY.md.",
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
        description="Deduplicate and trim durable memory in $K_AGENT_HOME/content/memory/MEMORY.md.",
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
