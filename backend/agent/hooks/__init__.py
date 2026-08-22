"""K Agent 进程内 Hook 的对外入口。

这里的 Hook **不是** Git Hook，也 **不是** Skill frontmatter 里的 ``hooks``
字段。Skill 的 ``hooks`` 只作为不可信文本交给模型，从不在本包执行。

分层约定：
- ``types``：请求级契约（事件、载荷、失败策略）
- ``decorators``：给函数贴元数据，**不**写入全局注册表
- ``middleware`` / ``observers``：可改执行 vs 只观测
- ``pipeline``：进程级 Definition 编译一次，每次 run bind 一份 Runtime
- ``builtins``：K Agent 默认启用的显式 Middleware 列表

``react_agent.OpenAIAgent`` 只面对 ``AgentPipelineRuntime``：观测失败不得打断
业务（fail-open），Middleware / 工具安全门失败会向上抛（fail-closed）。
"""

from backend.agent.hooks.decorators import (
    after_agent,
    after_model,
    before_agent,
    before_model,
    observe,
    wrap_model_call,
    wrap_tool_call,
)
from backend.agent.hooks.observers import ObserverDispatcher, TraceObserver
from backend.agent.hooks.pipeline import AgentPipelineDefinition, AgentPipelineRuntime
from backend.agent.hooks.types import (
    AgentCompletedEvent,
    AgentErrorPayload,
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    AgentStartedEvent,
    ContextBuiltEvent,
    ContextPlanPayload,
    ContextPrunedEvent,
    ContextPrunePayload,
    FailureMode,
    HookKind,
    HookSpec,
    ModelCallPayload,
    ModelCallCompleted,
    ModelCompletedEvent,
    ModelReasoningDelta,
    ModelResultPayload,
    ModelStreamEvent,
    ModelStartedEvent,
    ModelTextDelta,
    OperationFailedEvent,
    ToolCallPayload,
    ToolCallRequest,
    ToolCallResult,
    ToolCompletedEvent,
    ToolResultPayload,
    ToolStartedEvent,
)

__all__ = [
    "AgentCompletedEvent",
    "AgentErrorPayload",
    "AgentEvent",
    "AgentEventType",
    "AgentPipelineDefinition",
    "AgentPipelineRuntime",
    "AgentRunContext",
    "AgentStartedEvent",
    "ContextBuiltEvent",
    "ContextPlanPayload",
    "ContextPrunedEvent",
    "ContextPrunePayload",
    "FailureMode",
    "HookKind",
    "HookSpec",
    "ModelCallPayload",
    "ModelCallCompleted",
    "ModelCompletedEvent",
    "ModelReasoningDelta",
    "ModelResultPayload",
    "ModelStreamEvent",
    "ModelStartedEvent",
    "ModelTextDelta",
    "ObserverDispatcher",
    "OperationFailedEvent",
    "ToolCallPayload",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolCompletedEvent",
    "ToolResultPayload",
    "ToolStartedEvent",
    "TraceObserver",
    "after_agent",
    "after_model",
    "before_agent",
    "before_model",
    "observe",
    "wrap_model_call",
    "wrap_tool_call",
]
