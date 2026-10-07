"""显式工厂装配；只构建一次本轮 ToolSet，不创建待替换的占位绑定。"""

from backend.tools.builtins import LOCAL_TOOL_FACTORIES
from backend.tools.adapters.mcp import build_mcp_resource_tools
from backend.tools.adapters.skill import build_skill_tool
from backend.tools.adapters.user_input import ASK_USER_QUESTION_TOOL
from backend.tools.adapters.mcp import bind_mcp_tool, mcp_outcome
from backend.tools.adapters.user_input import bind_user_input
from backend.tools.toolset import RequestToolSet

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
        "InstallSandbox",
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
        "TodoWrite",
        "AskUserQuestion",
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


def build_request_tool_set(
    *, settings, mcp_tools, mcp_manager, skill_catalog, authorized_servers
):
    async def call_prompt(server, name, arguments):
        if server not in authorized_servers:
            raise ValueError(f"Unauthorized MCP server: {server}")
        return mcp_outcome(await mcp_manager.call_prompt(server, name, arguments))

    async def list_resources():
        resources = await mcp_manager.list_resources()
        return {server: items for server, items in resources.items() if server in authorized_servers}

    async def read_resource(server, uri):
        if server not in authorized_servers:
            raise ValueError(f"Unauthorized MCP server: {server}")
        return mcp_outcome(await mcp_manager.read_resource(server, uri))

    factories = (
        *LOCAL_TOOL_FACTORIES,
        *build_mcp_resource_tools(list_resources, read_resource),
        build_skill_tool(call_prompt, skill_catalog=skill_catalog),
    )
    bindings = [factory() for factory in factories]
    bindings.append(bind_user_input(ASK_USER_QUESTION_TOOL))
    # 全量冻结成目录：校验重名/schema，再按 preset 从这份目录挑，避免没选中的重复定义漏检。
    available = RequestToolSet.create(bindings)
    raw = settings.local_tool_names
    names = [n.strip() for n in raw.split(",") if n.strip()] if raw else []
    if not names:
        if settings.local_tool_preset not in TOOL_PRESETS:
            raise ValueError(f"Unknown tool preset: {settings.local_tool_preset}")
        names = TOOL_PRESETS[settings.local_tool_preset]
    selected = [available.resolve(name) for name in names]
    for descriptor in mcp_tools:
        if descriptor.server_id not in authorized_servers:
            raise ValueError(f"Unauthorized MCP server: {descriptor.server_id}")
        selected.append(bind_mcp_tool(descriptor, mcp_manager))
    return RequestToolSet.create(selected)
