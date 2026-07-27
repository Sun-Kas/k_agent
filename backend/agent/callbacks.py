from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from backend.schemas import ChatMessage


AgentEventType = Literal[
    "agent_start",
    "agent_end",
    "before_model",
    "after_model",
    "before_tool",
    "after_tool",
    "on_error",
]


@dataclass(slots=True)
class AgentRunContext:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelCallPayload:
    iteration: int
    model: str
    messages: list[ChatMessage]
    tools: list[dict[str, Any]]


@dataclass(slots=True)
class ModelResultPayload:
    iteration: int
    model: str
    response_id: str
    output_text: str
    function_call_count: int
    elapsed_ms: float


@dataclass(slots=True)
class ToolCallPayload:
    iteration: int
    name: str
    arguments: dict[str, Any]
    source: Literal["local", "mcp"]
    server_id: str | None = None


@dataclass(slots=True)
class ToolResultPayload:
    iteration: int
    name: str
    arguments: dict[str, Any]
    output: str
    source: Literal["local", "mcp"]
    elapsed_ms: float
    server_id: str | None = None


@dataclass(slots=True)
class AgentErrorPayload:
    error: Exception
    stage: str
    detail: dict[str, Any] = field(default_factory=dict)


class AgentCallback(Protocol):
    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        ...

    async def before_model(
        self,
        context: AgentRunContext,
        payload: ModelCallPayload,
    ) -> None:
        ...

    async def after_model(
        self,
        context: AgentRunContext,
        payload: ModelResultPayload,
    ) -> None:
        ...

    async def before_tool(
        self,
        context: AgentRunContext,
        payload: ToolCallPayload,
    ) -> None:
        ...

    async def after_tool(
        self,
        context: AgentRunContext,
        payload: ToolResultPayload,
    ) -> None:
        ...

    async def on_error(
        self,
        context: AgentRunContext,
        payload: AgentErrorPayload,
    ) -> None:
        ...

    async def on_agent_end(
        self,
        context: AgentRunContext,
        result: dict[str, Any],
    ) -> None:
        ...


Hook = Callable[[AgentRunContext, Any], Awaitable[None]]


class CallbackManager:
    def __init__(self, callbacks: list[AgentCallback] | None = None):
        self.callbacks = callbacks or []

    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        await self._emit("on_agent_start", context, messages)

    async def before_model(self, context: AgentRunContext, payload: ModelCallPayload) -> None:
        await self._emit("before_model", context, payload)

    async def after_model(self, context: AgentRunContext, payload: ModelResultPayload) -> None:
        await self._emit("after_model", context, payload)

    async def before_tool(self, context: AgentRunContext, payload: ToolCallPayload) -> None:
        await self._emit("before_tool", context, payload)

    async def after_tool(self, context: AgentRunContext, payload: ToolResultPayload) -> None:
        await self._emit("after_tool", context, payload)

    async def on_error(self, context: AgentRunContext, payload: AgentErrorPayload) -> None:
        await self._emit("on_error", context, payload)

    async def on_agent_end(self, context: AgentRunContext, result: dict[str, Any]) -> None:
        await self._emit("on_agent_end", context, result)

    async def _emit(self, method_name: str, context: AgentRunContext, payload: Any) -> None:
        for callback in self.callbacks:
            method = getattr(callback, method_name, None)
            if method is not None:
                await method(context, payload)


class TraceCallback:
    def __init__(self, trace: list[str]):
        self.trace = trace

    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        self.trace.append(f"agent:start:{context.run_id}:{len(messages)} messages")

    async def before_model(self, context: AgentRunContext, payload: ModelCallPayload) -> None:  # noqa: ARG002
        self.trace.append(f"model:before:{payload.model}:iteration:{payload.iteration}")

    async def after_model(self, context: AgentRunContext, payload: ModelResultPayload) -> None:  # noqa: ARG002
        self.trace.append(
            f"model:after:{payload.response_id}:{payload.function_call_count} tool calls"
        )

    async def before_tool(self, context: AgentRunContext, payload: ToolCallPayload) -> None:  # noqa: ARG002
        self.trace.append(f"tool:before:{payload.source}:{payload.name}")

    async def after_tool(self, context: AgentRunContext, payload: ToolResultPayload) -> None:  # noqa: ARG002
        self.trace.append(f"tool:after:{payload.source}:{payload.name}:{payload.elapsed_ms:.0f}ms")

    async def on_error(self, context: AgentRunContext, payload: AgentErrorPayload) -> None:  # noqa: ARG002
        self.trace.append(f"error:{payload.stage}:{payload.error}")

    async def on_agent_end(self, context: AgentRunContext, result: dict[str, Any]) -> None:  # noqa: ARG002
        elapsed_ms = (time.time() - context.started_at) * 1000
        self.trace.append(f"agent:end:{context.run_id}:{elapsed_ms:.0f}ms")
