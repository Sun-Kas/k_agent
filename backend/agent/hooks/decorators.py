"""Declarative decorators for trusted in-process Agent extensions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from backend.agent.hooks.types import AgentEventType, FailureMode, HookKind, HookSpec


CallableT = TypeVar("CallableT", bound=Callable[..., Any])
_HOOK_SPEC_ATTR = "__k_agent_hook_spec__"


def _decorate(
    kind: HookKind,
    *,
    order: int,
    name: str | None,
    failure_mode: FailureMode,
    event_type: AgentEventType | None = None,
    timeout_seconds: float | None = None,
) -> Callable[[CallableT], CallableT]:
    """Attach immutable metadata without mutating a process-global registry."""

    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("Hook timeout_seconds must be greater than zero")

    def decorate(func: CallableT) -> CallableT:
        if hasattr(func, _HOOK_SPEC_ATTR):
            raise ValueError(f"Hook {func.__name__!r} is already decorated")
        resolved_name = (name or func.__name__).strip()
        if not resolved_name:
            raise ValueError("Hook name must not be empty")
        setattr(
            func,
            _HOOK_SPEC_ATTR,
            HookSpec(
                kind=kind,
                name=resolved_name,
                order=order,
                failure_mode=failure_mode,
                event_type=event_type,
                timeout_seconds=timeout_seconds,
            ),
        )
        return func

    return decorate


def hook_spec(value: Any) -> HookSpec | None:
    """Return decorator metadata without triggering discovery or imports."""

    spec = getattr(value, _HOOK_SPEC_ATTR, None)
    return spec if isinstance(spec, HookSpec) else None


def observe(
    event_type: AgentEventType,
    *,
    order: int = 100,
    name: str | None = None,
    timeout_seconds: float | None = None,
) -> Callable[[CallableT], CallableT]:
    return _decorate(
        HookKind.OBSERVER,
        order=order,
        name=name,
        failure_mode=FailureMode.OPEN,
        event_type=event_type,
        timeout_seconds=timeout_seconds,
    )


def before_agent(*, order: int = 100, name: str | None = None):
    return _decorate(
        HookKind.BEFORE_AGENT,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def after_agent(*, order: int = 100, name: str | None = None):
    return _decorate(
        HookKind.AFTER_AGENT,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def before_model(*, order: int = 100, name: str | None = None):
    return _decorate(
        HookKind.BEFORE_MODEL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def after_model(*, order: int = 100, name: str | None = None):
    return _decorate(
        HookKind.AFTER_MODEL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def wrap_model_call(*, order: int = 100, name: str | None = None):
    return _decorate(
        HookKind.WRAP_MODEL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def wrap_tool_call(*, order: int = 100, name: str | None = None):
    return _decorate(
        HookKind.WRAP_TOOL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )
