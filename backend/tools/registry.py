from __future__ import annotations

from collections.abc import Iterable

from backend.config import get_or_init_settings
from backend.mcp_tool import McpClientManager
from backend.tools.cc_extra import CC_EXTRA_TOOLS, build_mcp_resource_tools
from backend.tools.cc_like import CC_LIKE_TOOLS
from backend.tools.local import LEGACY_TOOLS, ToolDefinition, build_skill_tool


TOOL_PRESETS: dict[str, tuple[str, ...]] = {
    "legacy": (
        "get_current_time",
        "echo_text",
        "read_personal_memory",
        "append_personal_memory",
        "search_personal_memory",
        "compact_personal_memory",
        "Skill",
    ),
    "coding": (
        "Read",
        "Write",
        "Edit",
        "Glob",
        "LS",
        "Grep",
        "Bash",
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
        "TodoWrite",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "get_current_time",
        "read_personal_memory",
        "append_personal_memory",
        "search_personal_memory",
        "compact_personal_memory",
        "Skill",
    ),
}


def get_all_base_tools() -> list[ToolDefinition]:
    """返回所有可能启用的本地工具；实际暴露前还会经过 preset/name 过滤。"""
    return _uniq_by_name([*CC_LIKE_TOOLS, *CC_EXTRA_TOOLS, *build_mcp_resource_tools(), *LEGACY_TOOLS, build_skill_tool()])


async def load_local_tools() -> list[ToolDefinition]:
    settings = await get_or_init_settings()
    all_tools = {tool.name: tool for tool in get_all_base_tools()}
    names = _configured_names(settings.local_tool_names)
    if names is None:
        names = list(TOOL_PRESETS.get(settings.local_tool_preset, TOOL_PRESETS["coding"]))
    return [all_tools[name] for name in names if name in all_tools]


def replace_skill_tool(tools: Iterable[ToolDefinition], skill_tool: ToolDefinition) -> list[ToolDefinition]:
    return [skill_tool if tool.name == "Skill" else tool for tool in tools]


def bind_request_scoped_tools(tools: Iterable[ToolDefinition], mcp_manager: McpClientManager) -> list[ToolDefinition]:
    """把依赖请求级 MCP manager 的工具换成闭包实例，避免跨请求复用连接状态。"""
    bound_mcp_tools = {
        tool.name: tool
        for tool in build_mcp_resource_tools(mcp_manager.list_resources, mcp_manager.read_resource)
    }
    result = []
    for tool in tools:
        if tool.name == "Skill":
            result.append(build_skill_tool(mcp_manager.call_prompt))
        elif tool.name in bound_mcp_tools:
            result.append(bound_mcp_tools[tool.name])
        else:
            result.append(tool)
    return result


def _configured_names(raw_names: str | None) -> list[str] | None:
    if raw_names is None:
        return None
    names = [item.strip() for item in raw_names.split(",") if item.strip()]
    return names or None


def _uniq_by_name(tools: Iterable[ToolDefinition]) -> list[ToolDefinition]:
    seen: set[str] = set()
    result: list[ToolDefinition] = []
    for tool in tools:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        result.append(tool)
    return result
