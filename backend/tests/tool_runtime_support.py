"""旧行为回归使用的测试装配器；生产 Agent 只接受预构建 ToolSet。"""

import json
from unittest.mock import AsyncMock
from backend.agent.react_agent import OpenAIAgent
from backend.tools.contracts import ToolOutcome
from backend.tools.factory import define_tool
from backend.tools.toolset import RequestToolSet
from backend.tools.catalog import SkillCatalog
from backend.tools.dispatcher import ToolDispatcher
from backend.tools.adapters.skill import SkillAliasNormalizer
from backend.tools.adapters.mcp import bind_mcp_tool, mcp_outcome
from backend.tools.adapters.user_input import bind_user_input


def tool_json(value):
    return json.loads(value.public_content if isinstance(value, ToolOutcome) else value)


def make_test_tool(name, description, parameters, execute, context_policy=None):
    async def typed(context, arguments):
        result = await execute(arguments)
        return result if isinstance(result, ToolOutcome) else mcp_outcome(result)

    from backend.tools.builtins import LOCAL_TOOL_FACTORIES

    registered = next(
        (f for f in LOCAL_TOOL_FACTORIES if f.spec.provider_name == name), None
    )
    subjects = registered.permission_subjects if registered else None
    return define_tool(
        name,
        description,
        parameters or {"type": "object"},
        typed,
        context_policy,
        permission_subjects=subjects,
    )


def test_capability_view(*, local_tools, mcp_tools):
    manager = AsyncMock()
    return RequestToolSet.create(
        [*[t() for t in local_tools], *[bind_mcp_tool(t, manager) for t in mcp_tools]]
    ).capability_view()


class ToolTestAgent(OpenAIAgent):
    __slots__ = ()

    async def create_runtime(self, request, tools, manager, *, skills=None, **kwargs):
        snapshot = await manager.list_tools()
        if request.mcp_server_ids:
            snapshot = [t for t in snapshot if t.server_id in request.mcp_server_ids]
        bindings = [
            bind_user_input(t) if t.spec.provider_name == "AskUserQuestion" else t()
            for t in tools
        ]
        tool_set = RequestToolSet.create(
            [*bindings, *[bind_mcp_tool(t, manager) for t in snapshot]]
        )
        dispatcher = ToolDispatcher(
            tool_set, normalizer=SkillAliasNormalizer(SkillCatalog.from_skills(skills or []))
        )
        return await super().create_runtime(request, tool_set, dispatcher, **kwargs)


from backend.tools.adapters.skill import (
    build_skill_tool as _build_skill_tool,
    invoke_skill as _invoke_skill,
)


def build_test_skill_tool(mcp_prompt_caller=None, skills=None, *, skill_catalog=None):
    async def prompt(server, name, arguments):
        return mcp_outcome(await mcp_prompt_caller(server, name, arguments))

    return _build_skill_tool(
        prompt if mcp_prompt_caller else None,
        skill_catalog=skill_catalog or SkillCatalog.from_skills(skills or []),
    )


async def invoke_test_skill(payload, skills):
    return await _invoke_skill(
        await builtin_context(), payload, SkillCatalog.from_skills(skills)
    )


async def builtin_context():
    from pathlib import Path
    from backend.config import get_or_init_settings
    from backend.tools.contracts import ToolExecutionContext, ToolOutputLimits
    from backend.tools.workspace import (
        current_tool_workspace,
        current_tool_network_access,
        current_tool_permission_mode,
    )

    settings = await get_or_init_settings()
    return ToolExecutionContext(
        workspace=current_tool_workspace()
        or Path(settings.local_tool_workspace_root).resolve(),
        network_access=current_tool_network_access(),
        permission_mode=current_tool_permission_mode(),
        output_limits=ToolOutputLimits(
            settings.local_tool_max_output_chars,
            settings.local_tool_bash_timeout_seconds,
        ),
    )


async def invoke_builtin(executor, payload):
    return await executor(await builtin_context(), payload)
