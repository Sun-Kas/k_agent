"""Middleware handler contracts shared by the compiled Agent pipeline."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeAlias

from backend.agent.hooks.types import (
    ModelCallPayload,
    ModelStreamEvent,
    ToolCallRequest,
    ToolCallResult,
)


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
