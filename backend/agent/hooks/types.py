"""Hook / Middleware 使用的类型化、请求级契约。

设计目标：
- 事件和模型/工具请求尽量 ``frozen``，避免 Observer 或 Middleware 改到共享对象
- ``AgentRunContext`` 是少数可变容器，且 **一次 HTTP run 一份**，防止同进程
  并发会话互相污染（尤其是 Skill 白名单）
- Observer 只消费这些快照，不能靠返回值改 Agent 行为
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeAlias

from backend.api.schemas import ChatMessage


class AgentEventType(str, Enum):
    """Observer 事件名。取值稳定，供日志、Langfuse 和 ``@observe`` 过滤使用。"""

    AGENT_STARTED = "agent_started"
    CONTEXT_BUILT = "context_built"
    CONTEXT_PRUNED = "context_pruned"
    MODEL_STARTED = "model_started"
    MODEL_COMPLETED = "model_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    OPERATION_FAILED = "operation_failed"
    AGENT_COMPLETED = "agent_completed"


class HookKind(str, Enum):
    """编译进 Pipeline 的扩展点种类。

    ``OBSERVER`` 不编进 Definition，只在 ``bind_runtime`` 时注入，因为日志 /
    Langfuse 带着 ``request_id`` 等请求级身份。
    """

    OBSERVER = "observer"
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    WRAP_MODEL = "wrap_model"
    WRAP_TOOL = "wrap_tool"


class FailureMode(str, Enum):
    """扩展失败是否允许影响这次业务执行。

    - ``OPEN``：观测失败只记 warning，run 继续（Observer 默认）
    - ``CLOSED``：异常向上抛，视为执行失败（Middleware 默认）
    """

    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class HookSpec:
    """装饰器贴到函数上的不可变元数据；不是全局注册表里的条目。"""

    kind: HookKind
    name: str
    order: int = 100
    failure_mode: FailureMode = FailureMode.CLOSED
    event_type: AgentEventType | None = None
    timeout_seconds: float | None = None


@dataclass(slots=True)
class AgentRunContext:
    """恰好属于一次 Agent run 的身份与可变状态。

    ``KAgentRunner`` / ``OpenAIAgent.create_runtime`` 每次 run 新建一份。
    进程级 ``AgentPipelineDefinition`` 不能持有这些字段，否则并发会话会串数据。
    """

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str = ""
    thread_id: str = ""
    agent_execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Skill 收窄是请求状态。放在这里可避免同进程、同 Agent 实例上的并发 run
    # 互相覆盖白名单。
    skill_allowlist: set[str] | None = None
    skill_allowlist_owner: str = "skill"


@dataclass(frozen=True, slots=True)
class ModelCallPayload:
    """暴露给 Middleware / Observer 的逻辑模型请求（不含流式增量）。"""

    iteration: int
    model: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    reasoning_effort: str | None = None
    operation_id: str = ""

    def override(self, **changes: Any) -> "ModelCallPayload":
        """返回新快照，禁止原地改共享 messages/tools。"""

        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ContextPlanPayload:
    """上下文预算摘要。只含计数/预算，不含消息正文，避免观测层泄露 prompt。"""

    input_message_count: int
    active_message_count: int
    provider_message_count: int
    compacted_message_count: int
    summary_chars: int
    attachment_count: int
    auto_compacted: bool
    budget: Mapping[str, int]
    breakdown: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ContextPrunePayload:
    """某次模型迭代里，旧工具输出被裁剪后的统计。"""

    iteration: int
    pruned_output_count: int
    before_chars: int
    after_chars: int


@dataclass(frozen=True, slots=True)
class ModelResultPayload:
    """提供商流被完整消费后的聚合结果（给 Observer / after_model）。"""

    iteration: int
    model: str
    response_id: str
    output_text: str
    function_call_count: int
    elapsed_ms: float
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    operation_id: str = ""
    input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelReasoningDelta:
    """一条按到达顺序保留的 reasoning 增量。"""

    content: str


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """一条按到达顺序保留的、对用户可见的文本增量。"""

    content: str


@dataclass(frozen=True, slots=True)
class ModelCallCompleted:
    """一次成功消费完的提供商流的终结标记；Middleware 必须最终 yield 它。"""

    result: ModelResultPayload


# 模型流事件三态：思考增量 / 可见文本 / 终结聚合。出现增量后禁止再重试。
ModelStreamEvent: TypeAlias = ModelReasoningDelta | ModelTextDelta | ModelCallCompleted


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """逻辑工具请求。重试保持 ``call_id``，每次物理尝试另发 ``operation_id``。"""

    call_id: str
    iteration: int
    requested_name: str
    canonical_name: str
    arguments: Mapping[str, Any]
    source: Literal["local", "mcp"]
    server_id: str | None = None

    def __post_init__(self) -> None:
        # 拷贝顶层 mapping，避免 Middleware 通过「frozen dataclass + 可变 dict」
        # 改到模型侧仍在使用的参数对象。
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def override(self, **changes: Any) -> "ToolCallRequest":
        """返回新请求；Pipeline 会让它重新经过 sealed 安全门，不能绕过权限。"""

        if "arguments" in changes:
            changes["arguments"] = dict(changes["arguments"])
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ToolCallPayload:
    """一次物理工具尝试，在校验/执行前立刻发给 Observer。"""

    iteration: int
    name: str
    arguments: Mapping[str, Any]
    source: Literal["local", "mcp"]
    server_id: str | None = None
    call_id: str = ""
    operation_id: str = ""
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class ToolResultPayload:
    """一次物理工具尝试成功后的观测载荷。"""

    iteration: int
    name: str
    arguments: Mapping[str, Any]
    output: str
    source: Literal["local", "mcp"]
    elapsed_ms: float
    server_id: str | None = None
    call_id: str = ""
    operation_id: str = ""
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """给 wrap_tool Middleware 的成功返回值（含本次 attempt / operation_id）。"""

    request: ToolCallRequest
    output: str
    elapsed_ms: float
    operation_id: str
    attempt: int


@dataclass(frozen=True, slots=True)
class AgentErrorPayload:
    """失败元数据。Observer 自行决定导出多少细节，dispatcher 日志不得带正文。"""

    error: BaseException
    stage: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    operation_id: str = ""
    parent_operation_id: str = ""


@dataclass(frozen=True, slots=True)
class AgentStartedEvent:
    """before_agent 成功后发出；messages 是进入循环时的快照。"""

    context: AgentRunContext
    messages: tuple[ChatMessage, ...]
    type: AgentEventType = field(default=AgentEventType.AGENT_STARTED, init=False)


@dataclass(frozen=True, slots=True)
class ContextBuiltEvent:
    """上下文预算规划完成（不含消息正文）。"""

    context: AgentRunContext
    payload: ContextPlanPayload
    type: AgentEventType = field(default=AgentEventType.CONTEXT_BUILT, init=False)


@dataclass(frozen=True, slots=True)
class ContextPrunedEvent:
    """本轮模型调用前裁剪了旧工具输出。"""

    context: AgentRunContext
    payload: ContextPrunePayload
    type: AgentEventType = field(default=AgentEventType.CONTEXT_PRUNED, init=False)


@dataclass(frozen=True, slots=True)
class ModelStartedEvent:
    """sealed 模型终端即将调用提供商；此时已分配本次 ``operation_id``。"""

    context: AgentRunContext
    payload: ModelCallPayload
    type: AgentEventType = field(default=AgentEventType.MODEL_STARTED, init=False)


@dataclass(frozen=True, slots=True)
class ModelCompletedEvent:
    """提供商流已完整消费并得到聚合结果。"""

    context: AgentRunContext
    payload: ModelResultPayload
    type: AgentEventType = field(default=AgentEventType.MODEL_COMPLETED, init=False)


@dataclass(frozen=True, slots=True)
class ToolStartedEvent:
    """preflight 通过后、execute 之前。权限拒绝不会发这条。"""

    context: AgentRunContext
    payload: ToolCallPayload
    type: AgentEventType = field(default=AgentEventType.TOOL_STARTED, init=False)


@dataclass(frozen=True, slots=True)
class ToolCompletedEvent:
    """工具 execute 成功。异常走 ``OperationFailedEvent``。"""

    context: AgentRunContext
    payload: ToolResultPayload
    type: AgentEventType = field(default=AgentEventType.TOOL_COMPLETED, init=False)


@dataclass(frozen=True, slots=True)
class OperationFailedEvent:
    """任一 fail-closed 阶段失败。stage 区分 before/wrap/preflight/execute 等。"""

    context: AgentRunContext
    payload: AgentErrorPayload
    type: AgentEventType = field(default=AgentEventType.OPERATION_FAILED, init=False)


@dataclass(frozen=True, slots=True)
class AgentCompletedEvent:
    """循环正常交出最终 result 后发出；随后才跑 after_agent。"""

    context: AgentRunContext
    result: Mapping[str, Any]
    type: AgentEventType = field(default=AgentEventType.AGENT_COMPLETED, init=False)


AgentEvent: TypeAlias = (
    AgentStartedEvent
    | ContextBuiltEvent
    | ContextPrunedEvent
    | ModelStartedEvent
    | ModelCompletedEvent
    | ToolStartedEvent
    | ToolCompletedEvent
    | OperationFailedEvent
    | AgentCompletedEvent
)
