"""Fail-open, deterministic dispatch for Agent observation events."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from backend.agent.hooks.decorators import hook_spec
from backend.agent.hooks.types import (
    AgentCompletedEvent,
    AgentEvent,
    AgentEventType,
    AgentStartedEvent,
    HookKind,
    ModelCompletedEvent,
    ModelStartedEvent,
    OperationFailedEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)


LOGGER = logging.getLogger("k_agent.agent.observers")


class AgentObserver(Protocol):
    """Request-scoped observer that cannot alter Agent execution."""

    async def handle(self, event: AgentEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class _ObserverEntry:
    observer: Any
    name: str
    order: int
    registration_index: int
    event_type: AgentEventType | None
    timeout_seconds: float | None


class ObserverDispatcher:
    """Dispatch observers sequentially and isolate every observer failure."""

    def __init__(self, observers: list[Any] | tuple[Any, ...] = ()) -> None:
        entries: list[_ObserverEntry] = []
        names: set[str] = set()
        for index, observer in enumerate(observers):
            if observer is None:
                continue
            spec = hook_spec(observer)
            if spec is not None and spec.kind is not HookKind.OBSERVER:
                raise TypeError(f"{spec.name!r} is middleware, not an observer")
            name = spec.name if spec is not None else type(observer).__name__
            if name in names:
                raise ValueError(f"Duplicate observer name: {name}")
            names.add(name)
            entries.append(
                _ObserverEntry(
                    observer=observer,
                    name=name,
                    order=spec.order if spec is not None else 100,
                    registration_index=index,
                    event_type=spec.event_type if spec is not None else None,
                    timeout_seconds=spec.timeout_seconds if spec is not None else None,
                )
            )
        self._entries = tuple(
            sorted(entries, key=lambda item: (item.order, item.registration_index))
        )

    async def emit(self, event: AgentEvent) -> None:
        """Emit one immutable event without allowing telemetry to break the run."""

        for entry in self._entries:
            if entry.event_type is not None and entry.event_type is not event.type:
                continue
            try:
                target = getattr(entry.observer, "handle", entry.observer)
                pending = target(event)
                if entry.timeout_seconds is None:
                    await pending
                else:
                    await asyncio.wait_for(pending, timeout=entry.timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never serialize the event or exception text here: either can
                # contain prompt/tool content supplied by the user or provider.
                LOGGER.warning(
                    "Agent observer failed: observer=%s event=%s error=%s",
                    entry.name,
                    event.type.value,
                    type(exc).__name__,
                )


class TraceObserver:
    """Keep the existing compact trace surface without observing request bodies."""

    def __init__(self, trace: list[str]) -> None:
        self._trace = trace

    async def handle(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartedEvent):
            self._trace.append(
                f"agent:start:{event.context.run_id}:{len(event.messages)} messages"
            )
        elif isinstance(event, ModelStartedEvent):
            payload = event.payload
            self._trace.append(
                f"model:before:{payload.model}:iteration:{payload.iteration}"
            )
        elif isinstance(event, ModelCompletedEvent):
            payload = event.payload
            self._trace.append(
                f"model:after:{payload.response_id}:{payload.function_call_count} tool calls"
            )
        elif isinstance(event, ToolStartedEvent):
            payload = event.payload
            self._trace.append(f"tool:before:{payload.source}:{payload.name}")
        elif isinstance(event, ToolCompletedEvent):
            payload = event.payload
            self._trace.append(
                f"tool:after:{payload.source}:{payload.name}:{payload.elapsed_ms:.0f}ms"
            )
        elif isinstance(event, OperationFailedEvent):
            payload = event.payload
            self._trace.append(f"error:{payload.stage}:{type(payload.error).__name__}")
        elif isinstance(event, AgentCompletedEvent):
            elapsed_ms = (time.time() - event.context.started_at) * 1000
            self._trace.append(
                f"agent:end:{event.context.run_id}:{elapsed_ms:.0f}ms"
            )
