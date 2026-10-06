"""工具运行时契约；公开正文与控制面状态彼此独立。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(value)
    return value


def thaw(value: Any) -> Any:
    """把 freeze 后的只读嵌套结构拷回普通 dict/list，方便 jsonschema、MCP、工具函数改副本。

    MappingProxyType 也是 Mapping，会变成 dict；tuple 变成 list。字符串/数字原样返回。
    """

    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ToolKey:
    source: Literal["local", "mcp"]
    name: str
    namespace: str | None = None


@dataclass(frozen=True, slots=True)
class ToolContextPolicy:
    mode: Literal["retain", "rerunnable", "receipt"] = "retain"
    max_result_chars: int = 50_000


@dataclass(frozen=True, slots=True)
class ToolExecutionPolicy:
    side_effect: Literal["read", "write", "external", "control"] = "read"
    supports_live_output: bool = False
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    key: ToolKey
    provider_name: str
    provider_description: str
    input_schema: Mapping[str, Any]
    context_policy: ToolContextPolicy = ToolContextPolicy()
    execution_policy: ToolExecutionPolicy = ToolExecutionPolicy()

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", freeze(self.input_schema))


class ToolProtocolError(RuntimeError):
    """Executor/effect 违反内部契约，不属于模型参数错误。"""


class ToolOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    kind: Literal["input", "permission", "execution", "protocol"] = "execution"
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ActivateToolAllowlist:
    owner: str
    allowed: frozenset[ToolKey]

    def __post_init__(self):
        object.__setattr__(self, "allowed", frozenset(self.allowed))


@dataclass(frozen=True, slots=True)
class RecordInvokedSkill:
    skill_id: str


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    todos: tuple[Any, ...]

    def __post_init__(self):
        object.__setattr__(self, "todos", freeze(self.todos))


@dataclass(frozen=True, slots=True)
class UpdateWorkingSet:
    files: tuple[Any, ...]

    def __post_init__(self):
        object.__setattr__(self, "files", freeze(self.files))


@dataclass(frozen=True, slots=True)
class RecordLoadedMemoryPaths:
    paths: tuple[str, ...]


ToolEffect = (
    ActivateToolAllowlist
    | RecordInvokedSkill
    | UpdatePlan
    | UpdateWorkingSet
    | RecordLoadedMemoryPaths
)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    status: ToolOutcomeStatus
    model_content: str
    public_content: str
    error: ToolError | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    effects: tuple[ToolEffect, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze(self.metadata))
        object.__setattr__(self, "effects", tuple(self.effects))
        if self.status == ToolOutcomeStatus.SUCCEEDED and self.error is not None:
            raise ValueError("Successful outcome cannot contain an error")
        if self.status != ToolOutcomeStatus.SUCCEEDED and self.effects:
            raise ValueError("Only successful outcomes may carry effects")

    @classmethod
    def succeeded(
        cls,
        content: str,
        *,
        public_content: str | None = None,
        effects=(),
        metadata=None,
    ):
        return cls(
            ToolOutcomeStatus.SUCCEEDED,
            content,
            content if public_content is None else public_content,
            effects=tuple(effects),
            metadata=metadata or {},
        )

    @classmethod
    def failed(
        cls,
        *,
        code: str,
        message: str,
        kind="execution",
        retryable=False,
        content: str | None = None,
        status=ToolOutcomeStatus.FAILED,
    ):
        text = (
            content
            if content is not None
            else json.dumps(
                {"ok": False, "error": message, "errorType": code}, ensure_ascii=False
            )
        )
        return cls(status, text, text, ToolError(code, message, kind, retryable))


def json_outcome(payload: Any, *, effects=()) -> ToolOutcome:
    """业务对象在序列化前决定状态；不能反解析展示正文执行安全 effect。"""
    content = json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, dict) and (
        payload.get("ok") is False or payload.get("success") is False
    ):
        return ToolOutcome.failed(
            code=str(payload.get("errorType") or "ToolExecutionError"),
            message=str(payload.get("error") or "Tool execution failed"),
            content=content,
        )
    return ToolOutcome.succeeded(content, effects=effects)


@dataclass(frozen=True, slots=True)
class ToolOutputLimits:
    max_chars: int = 50_000
    bash_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ToolBindingDependencies:
    workspace: Path | None = None
    network_access: bool | None = None
    output_limits: ToolOutputLimits = ToolOutputLimits()


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    request_id: str = ""
    run_id: str = ""
    workspace: Path | None = None
    network_access: bool | None = None
    permission_mode: str = "default"
    output_limits: ToolOutputLimits = ToolOutputLimits()
    emit_output: Callable[..., None] | None = None


@dataclass(frozen=True, slots=True)
class ToolBinding:
    spec: ToolSpec
    execute: Callable[[ToolExecutionContext, Mapping[str, Any]], Awaitable[ToolOutcome]]
    permission_subjects: Callable[[Mapping[str, Any]], tuple[str, ...]]
    # preflight 里、鉴权/execute 之前；AskUserQuestion 在这里发 Interrupt 并挂起本轮。
    prepare: Callable[..., Awaitable[None]] | None = None
    # 另一次 Resume 请求：把用户答案/取消收成 Observation；无 resume 则按批准/拒绝处理。
    resume: Callable[..., ToolOutcome] | None = None


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    call_id: str
    iteration: int
    requested_name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """Middleware 只接触不可信调用，不持有 Binding 或 executor。"""

    call_id: str
    iteration: int
    requested_name: str
    canonical_name: str
    arguments: Mapping[str, Any]
    source: Literal["local", "mcp"]
    server_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "arguments", freeze(self.arguments))

    def override(self, **changes):
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    request: ToolCallRequest
    outcome: ToolOutcome
    elapsed_ms: float
    operation_id: str
    attempt: int

    @property
    def output(self) -> str:
        return self.outcome.public_content
