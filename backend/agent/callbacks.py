"""Agent 模型/工具生命周期的类型化观察回调（日志、Langfuse、trace）。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from backend.api.schemas import ChatMessage


AgentEventType = Literal[
    "agent_start",
    "context_built",
    "context_pruned",
    "agent_end",
    "before_model",
    "after_model",
    "before_tool",
    "after_tool",
    "on_error",
]


@dataclass(slots=True)
class AgentRunContext:
    """保存一次 Agent run 的运行 ID、开始时间和元数据。"""
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 已调用的 Skill 可能收窄后续可用工具。放在 run context 而非 agent 实例上，
    # 因为同一个 OpenAIAgent 可能并发服务多轮。
    skill_allowlist: set[str] | None = None
    skill_allowlist_owner: str = "skill"


@dataclass(slots=True)
class ModelCallPayload:
    """描述模型调用前的输入消息和工具 schema。"""
    iteration: int
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]


@dataclass(slots=True)
class ContextPlanPayload:
    """上下文预算与压缩摘要；故意不携带消息正文，避免回调侧泄漏大 payload。"""

    input_message_count: int
    active_message_count: int
    provider_message_count: int
    compacted_message_count: int
    summary_chars: int
    attachment_count: int
    auto_compacted: bool
    budget: dict[str, int]
    breakdown: dict[str, int]


@dataclass(slots=True)
class ContextPrunePayload:
    """单轮模型调用前，旧工具输出裁剪的统计。"""

    iteration: int
    pruned_output_count: int
    before_chars: int
    after_chars: int


@dataclass(slots=True)
class ModelResultPayload:
    """描述模型调用后的文本、工具调用数和耗时。"""
    iteration: int
    model: str
    response_id: str
    output_text: str
    function_call_count: int
    elapsed_ms: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ToolCallPayload:
    """描述工具调用前的名称、参数和来源。"""
    iteration: int
    name: str
    arguments: dict[str, Any]
    source: Literal["local", "mcp"]
    server_id: str | None = None


@dataclass(slots=True)
class ToolResultPayload:
    """描述工具调用后的输出、来源和耗时。"""
    iteration: int
    name: str
    arguments: dict[str, Any]
    output: str
    source: Literal["local", "mcp"]
    elapsed_ms: float
    server_id: str | None = None


@dataclass(slots=True)
class AgentErrorPayload:
    """描述 Agent 运行错误所在阶段和细节。"""
    error: Exception
    stage: str
    detail: dict[str, Any] = field(default_factory=dict)


class AgentCallback(Protocol):
    """定义 Agent 生命周期回调协议。"""
    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        """在 Agent run 开始时接收消息列表。"""
        ...

    async def before_model(
        self,
        context: AgentRunContext,
        payload: ModelCallPayload,
    ) -> None:
        """在模型调用前接收模型请求信息。"""
        ...

    async def on_context_built(
        self,
        context: AgentRunContext,
        payload: ContextPlanPayload,
    ) -> None:
        """接收上下文预算与压缩摘要（不含消息正文）。"""
        ...

    async def on_context_pruned(
        self,
        context: AgentRunContext,
        payload: ContextPrunePayload,
    ) -> None:
        """接收旧工具输出裁剪统计。"""
        ...

    async def after_model(
        self,
        context: AgentRunContext,
        payload: ModelResultPayload,
    ) -> None:
        """在模型调用后接收模型结果信息。"""
        ...

    async def before_tool(
        self,
        context: AgentRunContext,
        payload: ToolCallPayload,
    ) -> None:
        """在工具调用前接收工具请求信息。"""
        ...

    async def after_tool(
        self,
        context: AgentRunContext,
        payload: ToolResultPayload,
    ) -> None:
        """在工具调用后接收工具结果信息。"""
        ...

    async def on_error(
        self,
        context: AgentRunContext,
        payload: AgentErrorPayload,
    ) -> None:
        """在 Agent 运行出错时接收异常信息。"""
        ...

    async def on_agent_end(
        self,
        context: AgentRunContext,
        result: dict[str, Any],
    ) -> None:
        """在 Agent run 结束时接收最终结果。"""
        ...


class CallbackManager:
    """顺序分发 Agent 生命周期回调。"""
    def __init__(self, callbacks: list[AgentCallback] | None = None):
        """持有回调列表；缺实现的方法在 `_emit` 时静默跳过。"""
        self.callbacks = callbacks or []

    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        """在 Agent run 开始时接收消息列表。"""
        await self._emit("on_agent_start", context, messages)

    async def before_model(self, context: AgentRunContext, payload: ModelCallPayload) -> None:
        """在模型调用前接收模型请求信息。"""
        await self._emit("before_model", context, payload)

    async def on_context_built(
        self,
        context: AgentRunContext,
        payload: ContextPlanPayload,
    ) -> None:
        """分发上下文规划摘要。"""
        await self._emit("on_context_built", context, payload)

    async def on_context_pruned(
        self,
        context: AgentRunContext,
        payload: ContextPrunePayload,
    ) -> None:
        """分发旧工具输出裁剪统计。"""
        await self._emit("on_context_pruned", context, payload)

    async def after_model(self, context: AgentRunContext, payload: ModelResultPayload) -> None:
        """在模型调用后接收模型结果信息。"""
        await self._emit("after_model", context, payload)

    async def before_tool(self, context: AgentRunContext, payload: ToolCallPayload) -> None:
        """在工具调用前接收工具请求信息。"""
        await self._emit("before_tool", context, payload)

    async def after_tool(self, context: AgentRunContext, payload: ToolResultPayload) -> None:
        """在工具调用后接收工具结果信息。"""
        await self._emit("after_tool", context, payload)

    async def on_error(self, context: AgentRunContext, payload: AgentErrorPayload) -> None:
        """在 Agent 运行出错时接收异常信息。"""
        await self._emit("on_error", context, payload)

    async def on_agent_end(self, context: AgentRunContext, result: dict[str, Any]) -> None:
        """在 Agent run 结束时接收最终结果。"""
        await self._emit("on_agent_end", context, result)

    async def _emit(self, method_name: str, context: AgentRunContext, payload: Any) -> None:
        """按方法名调用所有实现该回调的对象。"""
        for callback in self.callbacks:
            method = getattr(callback, method_name, None)
            if method is not None:
                await method(context, payload)


class TraceCallback:
    """把 Agent 生命周期事件记录为 trace 字符串。"""
    def __init__(self, trace: list[str]):
        """写入调用方提供的共享 `trace` 列表，供最终状态回传。"""
        self.trace = trace

    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        """在 Agent run 开始时接收消息列表。"""
        self.trace.append(f"agent:start:{context.run_id}:{len(messages)} messages")

    async def before_model(self, context: AgentRunContext, payload: ModelCallPayload) -> None:  # noqa: ARG002
        """在模型调用前接收模型请求信息。"""
        self.trace.append(f"model:before:{payload.model}:iteration:{payload.iteration}")

    async def after_model(self, context: AgentRunContext, payload: ModelResultPayload) -> None:  # noqa: ARG002
        """在模型调用后接收模型结果信息。"""
        self.trace.append(
            f"model:after:{payload.response_id}:{payload.function_call_count} tool calls"
        )

    async def before_tool(self, context: AgentRunContext, payload: ToolCallPayload) -> None:  # noqa: ARG002
        """在工具调用前接收工具请求信息。"""
        self.trace.append(f"tool:before:{payload.source}:{payload.name}")

    async def after_tool(self, context: AgentRunContext, payload: ToolResultPayload) -> None:  # noqa: ARG002
        """在工具调用后接收工具结果信息。"""
        self.trace.append(f"tool:after:{payload.source}:{payload.name}:{payload.elapsed_ms:.0f}ms")

    async def on_error(self, context: AgentRunContext, payload: AgentErrorPayload) -> None:  # noqa: ARG002
        """在 Agent 运行出错时接收异常信息。"""
        self.trace.append(f"error:{payload.stage}:{payload.error}")

    async def on_agent_end(self, context: AgentRunContext, result: dict[str, Any]) -> None:  # noqa: ARG002
        """在 Agent run 结束时接收最终结果。"""
        elapsed_ms = (time.time() - context.started_at) * 1000
        self.trace.append(f"agent:end:{context.run_id}:{elapsed_ms:.0f}ms")
