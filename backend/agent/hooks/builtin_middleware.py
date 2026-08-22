"""K Agent 默认启用的可信 Middleware 实现。

新增 Middleware 时先在本文件定义，再到 ``hooks/builtins.py`` 的显式列表中注册。
Decorator 只声明类型和顺序，不会自动发现或挂载函数；这种显式注册可避免 Skill、
环境变量或导入副作用把不受信任代码带进 Agent 执行链。
"""

from __future__ import annotations

from backend.agent.hooks.decorators import wrap_tool_call
from backend.agent.hooks.middleware import AsyncToolCallHandler
from backend.agent.hooks.types import ToolCallRequest, ToolCallResult


_READ_ONLY_LOCAL_TOOLS = frozenset({"Read", "Glob", "Grep", "LS"})
_LEGACY_ESCALATION_FIELDS = frozenset(
    {"sandbox_permissions", "escalation_scope", "escalation_resource"}
)


@wrap_tool_call(order=100, name="strip_legacy_read_escalation_fields")
async def strip_legacy_read_escalation_fields(
    request: ToolCallRequest,
    call_next: AsyncToolCallHandler,
) -> ToolCallResult:
    """清理历史消息可能重放给只读工具的旧提权参数。

    这些字段不是 Read/Glob/Grep/LS 的业务参数。保留它们会导致 schema 校验失败，
    也可能让无副作用读取看起来像一次提权请求。Middleware 只重写类型化请求，随后
    必须调用 ``call_next``；Pipeline 会让新请求重新经过 sealed preflight、权限检查和
    schema 校验，因此这里不能绕过安全边界。
    """

    if request.source == "local" and request.canonical_name in _READ_ONLY_LOCAL_TOOLS:
        sanitized_arguments = {
            key: value
            for key, value in request.arguments.items()
            if key not in _LEGACY_ESCALATION_FIELDS
        }
        if len(sanitized_arguments) != len(request.arguments):
            request = request.override(arguments=sanitized_arguments)

    return await call_next(request)
