"""已选 Skill 的授权正文加载、兼容别名和类型化控制 effect。"""

from __future__ import annotations
from backend.tools.contracts import ToolExecutionContext
import asyncio
from pathlib import Path
from typing import Any
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool, LocalToolFactory
from backend.tools.contracts import ToolOutcome, json_outcome
from collections.abc import Awaitable, Callable
from backend.skills import SkillBodyError, load_skill_body
from backend.tools.catalog import SkillCatalog
from backend.tools.contracts import RecordInvokedSkill, ActivateToolAllowlist, ToolKey

SKILL_TOOL_DESCRIPTION = (
    "Load a K Agent skill or MCP prompt by exact name. Available K Agent skills "
    "are listed in the request context. Invoke a matching skill before continuing "
    "with the task; do not guess names."
)


async def invoke_skill(
    ctx: ToolExecutionContext, payload: dict[str, Any], skill_catalog: SkillCatalog
) -> ToolOutcome:
    """校验本轮 catalog 快照后，按 ID 懒加载 Skill 正文。"""
    # 容忍模型带上斜杠前缀（把 Skill 当斜杠命令写成 `/foo`）。
    skill_name = str(payload.get("skill", "")).strip().lstrip("/")
    args = str(payload.get("args", "")).strip()
    if not skill_name:
        return json_outcome({"success": False, "error": "skill is required"})
    # 先用本次请求的 catalog 快照做授权。只有匹配成功后才允许碰磁盘；
    # 文件只贡献正文，参数、白名单和触发条件仍以请求 metadata 为准。
    skill = next(
        (
            item
            for item in skill_catalog.items
            if skill_name in {str(item.get("id")), str(item.get("name"))}
        ),
        None,
    )
    if skill is None:
        return json_outcome({"success": False, "error": f"Unknown skill: {skill_name}"})
    if not skill.get("enabled", True) or skill.get("disableModelInvocation", False):
        return json_outcome(
            {
                "success": False,
                "error": f"Skill {skill_name} cannot be invoked by the model",
            }
        )
    try:
        # 文件读取发生在工具已授权之后，并移出流式事件循环，避免较大的正文阻塞 SSE。
        body = await asyncio.to_thread(
            load_skill_body,
            str(skill.get("id") or ""),
        )
    except SkillBodyError as exc:
        return json_outcome(
            {
                "success": False,
                "error": str(exc),
            }
        )
    content = _render_skill_content(
        body.instructions,
        args,
        tuple(str(value) for value in skill.get("argumentNames", [])),
        str(body.base_dir),
        ctx.workspace,
    )
    hook_notes = _render_skill_hooks(skill.get("hooks", {}))
    allowed = frozenset(
        ToolKey("mcp", "__".join(name.split("__")[2:]), name.split("__")[1])
        if name.startswith("mcp__") and len(name.split("__")) >= 3
        else ToolKey("local", name)
        for name in skill.get("allowedTools", ())
    )
    effects = (RecordInvokedSkill(str(skill.get("id") or skill_name)),)
    if allowed:
        effects += (
            ActivateToolAllowlist(
                str(skill.get("name") or skill_name),
                allowed | {ToolKey("local", "Skill")},
            ),
        )
    return json_outcome(
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
        effects=effects,
    )


def build_skill_tool(
    mcp_prompt_caller: Callable[[str, str, dict[str, Any]], Awaitable[ToolOutcome]]
    | None = None,
    *,
    skill_catalog: SkillCatalog,
) -> LocalToolFactory:
    """以闭包绑定本轮 MCP prompt 调用与 skills，避免跨请求复用连接。"""

    async def execute(
        ctx: ToolExecutionContext, payload: dict[str, Any]
    ) -> ToolOutcome:
        """Skill 入口：`mcp__` 前缀走 MCP prompt，否则走本地 Skill 定义。"""
        skill_name = str(payload.get("skill", "")).strip().lstrip("/")
        args = str(payload.get("args", "")).strip()
        # MCP server 暴露的 prompt 复用同一个 Skill 入口，靠 mcp__ 前缀区分，
        # 这样模型只需要认识一个工具，不必再学一套 prompt 调用协议。
        if skill_name.startswith("mcp__") and mcp_prompt_caller is not None:
            _, server_id, *prompt_parts = skill_name.split("__")
            prompt_name = "__".join(prompt_parts)
            return await mcp_prompt_caller(
                server_id, prompt_name, {"args": args} if args else {}
            )
        return await invoke_skill(ctx, payload, skill_catalog)

    skill_property: dict[str, Any] = {
        "type": "string",
        "description": "The skill or MCP prompt name, without a leading slash.",
    }
    # Do not add an enum: the same entry point also accepts dynamically listed
    # MCP prompt names, which are not part of the local SkillCatalog.

    return define_tool(
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
        execution_policy=ToolExecutionPolicy("control", supports_live_output=False),
        permission_subjects=lambda args: (str(args.get("skill") or "Skill"),),
    )


def _render_skill_content(
    content: str,
    args: str,
    argument_names: tuple[str, ...],
    base_dir: str | None,
    workspace: Path | None,
) -> str:
    """渲染 Skill 的正文、路径和使用条件。"""
    rendered = content.replace("$ARGUMENTS", args)
    for index, name in enumerate(argument_names):
        value = args.split()[index] if index < len(args.split()) else ""
        rendered = rendered.replace(f"${{{name}}}", value)
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


class SkillAliasNormalizer:
    """Provider 误把 Skill 名作为函数名时，只归一化本轮已授权目录中的别名。"""
    def __init__(self, catalog: SkillCatalog):
        self.catalog = catalog

    def __call__(self, name, arguments, tool_set):
        if name in tool_set.by_provider_name or "Skill" not in tool_set.by_provider_name:
            return name, arguments
        if any(name in {item.get("id"), item.get("name")} for item in self.catalog.items):
            from backend.tools.normalization import _skill_alias_arguments
            return "Skill", _skill_alias_arguments(name, arguments)
        return name, arguments
