"""MCP transport 的结果只在此适配；错误 content 保持外部协议兼容。"""

from backend.tools.contracts import ToolExecutionContext
import json
from backend.tools.contracts import (
    ToolBinding,
    ToolSpec,
    ToolKey,
    ToolOutcome,
    ToolExecutionPolicy,
    json_outcome,
    thaw,
)


def mcp_outcome(result) -> ToolOutcome:
    if isinstance(result, ToolOutcome):
        return result
    # MCP transport 是外部字符串协议，唯一允许的兼容解析边界。
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return ToolOutcome.succeeded(str(result))
    outcome = json_outcome(payload)
    if outcome.error:
        return ToolOutcome.failed(
            code=outcome.error.code, message=outcome.error.message, content=result
        )
    return ToolOutcome.succeeded(result)


def bind_mcp_tool(descriptor, manager):
    spec = ToolSpec(
        ToolKey("mcp", descriptor.name, descriptor.server_id),
        f"mcp__{descriptor.server_id}__{descriptor.name}",
        descriptor.description
        or f"MCP tool {descriptor.name} from {descriptor.server_id}",
        descriptor.input_schema or {"type": "object", "properties": {}},
        execution_policy=ToolExecutionPolicy("external"),
    )

    async def execute(context, arguments):
        return mcp_outcome(
            await manager.call_tool(spec.key.namespace, spec.key.name, thaw(arguments))
        )

    return ToolBinding(
        spec, execute, lambda args: (f"{spec.key.namespace}:{spec.key.name}",)
    )


from collections.abc import Callable, Awaitable
from typing import Any
from backend.tools.factory import define_tool, LocalToolFactory
from backend.tools.builtins.common import _json


def build_mcp_resource_tools(
    list_resources: Callable[[], Awaitable[dict[str, list[dict[str, Any]]]]]
    | None = None,
    read_resource: Callable[[str, str], Awaitable[ToolOutcome]] | None = None,
) -> list[LocalToolFactory]:
    """构造 MCP 资源工具；实际执行函数会在请求级 manager 绑定后注入。"""

    async def execute_list(ctx: ToolExecutionContext, _: dict[str, Any]) -> ToolOutcome:
        """列出 MCP resources 或 prompts。"""
        # 未绑定 manager 说明工具是从模块级注册表直接取的（例如统计工具数量），
        # 返回结构化错误而不是抛异常，模型看到后会转而使用其他手段。
        if list_resources is None:
            return _json({"ok": False, "error": "MCP manager is not bound"})
        return _json({"ok": True, "resources": await list_resources()})

    async def execute_read(
        ctx: ToolExecutionContext, payload: dict[str, Any]
    ) -> ToolOutcome:
        """读取指定 MCP resource 内容。"""
        if read_resource is None:
            return _json({"ok": False, "error": "MCP manager is not bound"})
        server_id = str(
            payload.get("server_id") or payload.get("serverId") or ""
        ).strip()
        uri = str(payload.get("uri") or "").strip()
        if not server_id or not uri:
            return _json({"ok": False, "error": "server_id and uri are required"})
        return await read_resource(server_id, uri)

    return [
        # 这两个工具对应 Claude Code 的 MCP resource 工具名，便于后续权限规则和提示词复用同一套名称。
        define_tool(
            name="ListMcpResourcesTool",
            description="List resources exposed by connected MCP servers.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            execute=execute_list,
            execution_policy=ToolExecutionPolicy(
                "external", supports_live_output=False
            ),
        ),
        define_tool(
            name="ReadMcpResourceTool",
            description="Read a resource from a connected MCP server by server_id and uri.",
            parameters={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "uri": {"type": "string"},
                },
                "required": ["server_id", "uri"],
                "additionalProperties": False,
            },
            execute=execute_read,
            execution_policy=ToolExecutionPolicy(
                "external", supports_live_output=False
            ),
            permission_subjects=lambda args: (
                f"{args.get('server_id') or ''}:{args.get('uri') or ''}",
            ),
        ),
    ]
