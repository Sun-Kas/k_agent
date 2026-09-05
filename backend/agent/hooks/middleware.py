"""编译后的 Agent Pipeline 共用的 Middleware 函数签名。

三类扩展：
- ``NodeHook``：before/after_*，读写 ``runtime.state``，可返回 dict 做增量合并
- ``ToolCallMiddleware``：``(request, call_next) -> result``，可改请求、短路、重试
- ``ModelCallMiddleware``：同样是 ``call_next``，但产出的是异步流

``call_next`` 指向更内层（最终是 Pipeline 焊死的 sealed 终端），Middleware
拿不到裸的模型/工具实现，因此不能绕过权限、Skill 白名单或参数 schema。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeAlias

from backend.agent.hooks.types import (
    ModelCallPayload,
    ModelStreamEvent,
    ToolCallRequest,
    ToolCallResult,
)


# before/after 钩子共享的可变黑板；一次 Runtime 一份，禁止跨 run 复用。
AgentState: TypeAlias = dict[str, Any]
NodeHook: TypeAlias = Callable[[AgentState, Any], Awaitable[dict[str, Any] | None]]
AsyncToolCallHandler: TypeAlias = Callable[[ToolCallRequest], Awaitable[ToolCallResult]]
ToolCallMiddleware: TypeAlias = Callable[
    [ToolCallRequest, AsyncToolCallHandler], Awaitable[ToolCallResult]
]


AsyncModelCallHandler: TypeAlias = Callable[
    [ModelCallPayload], AsyncIterator[ModelStreamEvent]
]
ModelCallMiddleware: TypeAlias = Callable[
    [ModelCallPayload, AsyncModelCallHandler], AsyncIterator[ModelStreamEvent]
]
