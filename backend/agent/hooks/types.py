"""Typed, request-scoped contracts used by Agent hooks and middleware."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeAlias

from backend.api.schemas import ChatMessage


class AgentEventType(str, Enum):
    """Stable observer event names."""

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
    """Execution extension points compiled into an Agent pipeline."""

    OBSERVER = "observer"
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    WRAP_MODEL = "wrap_model"
    WRAP_TOOL = "wrap_tool"


class FailureMode(str, Enum):
    """Whether an extension failure may affect the business operation."""

    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class HookSpec:
    """Immutable metadata attached by declarative decorators."""

    kind: HookKind
    name: str
    order: int = 100
    failure_mode: FailureMode = FailureMode.CLOSED
    event_type: AgentEventType | None = None
    timeout_seconds: float | None = None


@dataclass(slots=True)
class AgentRunContext:
    """Identity and mutable state belonging to exactly one Agent run."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str = ""
    thread_id: str = ""
    agent_execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Skill narrowing is request state. Keeping it here prevents concurrent runs
    # served by the same process-level Agent from contaminating each other.
    skill_allowlist: set[str] | None = None
    skill_allowlist_owner: str = "skill"


@dataclass(frozen=True, slots=True)
class ModelCallPayload:
    """Immutable logical model request exposed to middleware and observers."""

    iteration: int
    model: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    reasoning_effort: str | None = None
    operation_id: str = ""

    def override(self, **changes: Any) -> "ModelCallPayload":
        """Return a new request snapshot instead of mutating shared state."""

        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ContextPlanPayload:
    """Context budget summary without message bodies."""

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
    """Old tool-output pruning statistics for one model iteration."""

    iteration: int
    pruned_output_count: int
    before_chars: int
    after_chars: int


@dataclass(frozen=True, slots=True)
class ModelResultPayload:
    """Aggregated non-stream result after the provider stream is consumed."""

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
    """One provider reasoning delta in original arrival order."""

    content: str


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """One user-visible model text delta in original arrival order."""

    content: str


@dataclass(frozen=True, slots=True)
class ModelCallCompleted:
    """Terminal aggregate for one successfully consumed provider stream."""

    result: ModelResultPayload


ModelStreamEvent: TypeAlias = ModelReasoningDelta | ModelTextDelta | ModelCallCompleted


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """Logical tool request; retries keep call_id and create new operation IDs."""

    call_id: str
    iteration: int
    requested_name: str
    canonical_name: str
    arguments: Mapping[str, Any]
    source: Literal["local", "mcp"]
    server_id: str | None = None

    def __post_init__(self) -> None:
        # Copy the top-level mapping so middleware cannot mutate the model-owned
        # dictionary through an otherwise frozen dataclass.
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def override(self, **changes: Any) -> "ToolCallRequest":
        """Return a new request that will pass through the sealed safety gate."""

        if "arguments" in changes:
            changes["arguments"] = dict(changes["arguments"])
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ToolCallPayload:
    """One physical tool attempt emitted immediately before validation/execution."""

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
    """Successful result for one physical tool attempt."""

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
    """Middleware-facing successful tool result."""

    request: ToolCallRequest
    output: str
    elapsed_ms: float
    operation_id: str
    attempt: int


@dataclass(frozen=True, slots=True)
class AgentErrorPayload:
    """Failure metadata; observers decide how much detail is safe to export."""

    error: BaseException
    stage: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    operation_id: str = ""
    parent_operation_id: str = ""


@dataclass(frozen=True, slots=True)
class AgentStartedEvent:
    context: AgentRunContext
    messages: tuple[ChatMessage, ...]
    type: AgentEventType = field(default=AgentEventType.AGENT_STARTED, init=False)


@dataclass(frozen=True, slots=True)
class ContextBuiltEvent:
    context: AgentRunContext
    payload: ContextPlanPayload
    type: AgentEventType = field(default=AgentEventType.CONTEXT_BUILT, init=False)


@dataclass(frozen=True, slots=True)
class ContextPrunedEvent:
    context: AgentRunContext
    payload: ContextPrunePayload
    type: AgentEventType = field(default=AgentEventType.CONTEXT_PRUNED, init=False)


@dataclass(frozen=True, slots=True)
class ModelStartedEvent:
    context: AgentRunContext
    payload: ModelCallPayload
    type: AgentEventType = field(default=AgentEventType.MODEL_STARTED, init=False)


@dataclass(frozen=True, slots=True)
class ModelCompletedEvent:
    context: AgentRunContext
    payload: ModelResultPayload
    type: AgentEventType = field(default=AgentEventType.MODEL_COMPLETED, init=False)


@dataclass(frozen=True, slots=True)
class ToolStartedEvent:
    context: AgentRunContext
    payload: ToolCallPayload
    type: AgentEventType = field(default=AgentEventType.TOOL_STARTED, init=False)


@dataclass(frozen=True, slots=True)
class ToolCompletedEvent:
    context: AgentRunContext
    payload: ToolResultPayload
    type: AgentEventType = field(default=AgentEventType.TOOL_COMPLETED, init=False)


@dataclass(frozen=True, slots=True)
class OperationFailedEvent:
    context: AgentRunContext
    payload: AgentErrorPayload
    type: AgentEventType = field(default=AgentEventType.OPERATION_FAILED, init=False)


@dataclass(frozen=True, slots=True)
class AgentCompletedEvent:
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
