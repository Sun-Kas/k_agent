"""Compile declarative hooks and bind them to one request-scoped runtime."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from backend.agent.hooks.decorators import hook_spec
from backend.agent.hooks.middleware import (
    AsyncModelCallHandler,
    AsyncToolCallHandler,
    ModelCallMiddleware,
    NodeHook,
    ToolCallMiddleware,
)
from backend.agent.hooks.observers import ObserverDispatcher
from backend.agent.hooks.types import (
    AgentCompletedEvent,
    AgentErrorPayload,
    AgentRunContext,
    AgentStartedEvent,
    ContextBuiltEvent,
    ContextPlanPayload,
    ContextPrunedEvent,
    ContextPrunePayload,
    HookKind,
    ModelCallPayload,
    ModelCallCompleted,
    ModelReasoningDelta,
    ModelCompletedEvent,
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
from backend.api.schemas import ChatMessage


ToolPreflight = Callable[[ToolCallRequest], Awaitable[None]]
ToolExecutor = Callable[[ToolCallRequest], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class _CompiledHooks:
    before_agent: tuple[NodeHook, ...] = ()
    after_agent: tuple[NodeHook, ...] = ()
    before_model: tuple[NodeHook, ...] = ()
    after_model: tuple[NodeHook, ...] = ()
    wrap_tool: tuple[ToolCallMiddleware, ...] = ()
    wrap_model: tuple[ModelCallMiddleware, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentPipelineDefinition:
    """Immutable process-level hook definition with no request data."""

    compiled: _CompiledHooks = field(default_factory=_CompiledHooks)

    @classmethod
    def compile(cls, middleware: list[Any] | tuple[Any, ...] = ()) -> "AgentPipelineDefinition":
        """Validate explicit middleware and compile deterministic execution order."""

        grouped: dict[HookKind, list[tuple[int, int, Any]]] = {
            kind: [] for kind in HookKind if kind is not HookKind.OBSERVER
        }
        names: set[str] = set()
        for index, extension in enumerate(middleware):
            spec = hook_spec(extension)
            if spec is None or spec.kind is HookKind.OBSERVER:
                raise TypeError("Middleware must use a before/after/wrap decorator")
            if spec.name in names:
                raise ValueError(f"Duplicate middleware name: {spec.name}")
            names.add(spec.name)
            grouped[spec.kind].append((spec.order, index, extension))

        def ordered(kind: HookKind, *, reverse: bool = False) -> tuple[Any, ...]:
            values = sorted(grouped[kind], key=lambda item: (item[0], item[1]))
            if reverse:
                values.reverse()
            return tuple(value for _order, _index, value in values)

        return cls(
            _CompiledHooks(
                before_agent=ordered(HookKind.BEFORE_AGENT),
                after_agent=ordered(HookKind.AFTER_AGENT, reverse=True),
                before_model=ordered(HookKind.BEFORE_MODEL),
                after_model=ordered(HookKind.AFTER_MODEL, reverse=True),
                wrap_tool=ordered(HookKind.WRAP_TOOL),
                wrap_model=ordered(HookKind.WRAP_MODEL),
            )
        )

    def bind_runtime(
        self,
        *,
        context: AgentRunContext,
        observers: list[Any] | tuple[Any, ...] = (),
    ) -> "AgentPipelineRuntime":
        """Create isolated state and request-scoped observers for one run."""

        return AgentPipelineRuntime(
            context=context,
            observers=ObserverDispatcher(observers),
            compiled=self.compiled,
        )


@dataclass(slots=True)
class AgentPipelineRuntime:
    """One run's observer dispatcher, middleware state, and operation counters."""

    context: AgentRunContext
    observers: ObserverDispatcher
    compiled: _CompiledHooks
    state: dict[str, Any] = field(default_factory=dict)
    request_state: dict[str, Any] = field(default_factory=dict)
    _tool_attempts: dict[str, int] = field(default_factory=dict)

    async def emit_context_built(self, payload: ContextPlanPayload) -> None:
        await self.observers.emit(ContextBuiltEvent(self.context, payload))

    async def emit_context_pruned(self, payload: ContextPrunePayload) -> None:
        await self.observers.emit(ContextPrunedEvent(self.context, payload))

    def agent_run(
        self, messages: list[ChatMessage]
    ) -> "_AgentRunScope":
        return _AgentRunScope(self, messages)

    async def stream_model(
        self,
        request: ModelCallPayload,
        terminal: AsyncModelCallHandler,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Run node/wrap middleware around a backpressure-preserving model stream."""

        self.state["model_request"] = request
        try:
            await self._run_node_hooks(self.compiled.before_model)
        except BaseException as exc:
            await self.emit_failure(exc, stage="before_model")
            raise
        logical_request = self.state.get("model_request", request)
        if not isinstance(logical_request, ModelCallPayload):
            raise TypeError("before_model must leave a ModelCallPayload in model_request")

        failed_exceptions: set[int] = set()

        async def sealed(current: ModelCallPayload) -> AsyncIterator[ModelStreamEvent]:
            operation_request = current.override(operation_id=str(uuid.uuid4()))
            await self.observers.emit(ModelStartedEvent(self.context, operation_request))
            completed: ModelResultPayload | None = None
            try:
                async for event in terminal(operation_request):
                    if isinstance(event, ModelCallCompleted):
                        completed = event.result
                    yield event
            except BaseException as exc:
                failed_exceptions.add(id(exc))
                await self.emit_failure(
                    exc,
                    stage="model_call",
                    operation_id=operation_request.operation_id,
                    parent_operation_id=self.context.agent_execution_id,
                )
                raise
            if completed is None:
                error = RuntimeError("Model stream ended without ModelCallCompleted")
                failed_exceptions.add(id(error))
                await self.emit_failure(
                    error,
                    stage="model_call",
                    operation_id=operation_request.operation_id,
                )
                raise error
            await self.observers.emit(ModelCompletedEvent(self.context, completed))

        handler: AsyncModelCallHandler = sealed
        def compose_one(
            wrapper: ModelCallMiddleware,
            inner: AsyncModelCallHandler,
        ) -> AsyncModelCallHandler:
            committed = False
            calls = 0

            async def guarded_inner(
                current: ModelCallPayload,
            ) -> AsyncIterator[ModelStreamEvent]:
                nonlocal committed, calls
                if calls > 0 and committed:
                    raise RuntimeError(
                        "Model middleware cannot retry after a visible stream delta"
                    )
                calls += 1
                async for item in inner(current):
                    if isinstance(item, (ModelReasoningDelta, ModelTextDelta)):
                        committed = True
                    yield item

            async def composed(
                current: ModelCallPayload,
            ) -> AsyncIterator[ModelStreamEvent]:
                async for item in wrapper(current, guarded_inner):
                    yield item

            return composed

        for wrapper in reversed(self.compiled.wrap_model):
            handler = compose_one(wrapper, handler)

        completed: ModelResultPayload | None = None
        try:
            async for event in handler(logical_request):
                if isinstance(event, ModelCallCompleted):
                    completed = event.result
                yield event
        except BaseException as exc:
            if id(exc) not in failed_exceptions:
                await self.emit_failure(exc, stage="model_middleware")
            raise
        if completed is None:
            error = RuntimeError("Model middleware ended without ModelCallCompleted")
            await self.emit_failure(error, stage="model_middleware")
            raise error
        self.state["model_result"] = completed
        try:
            await self._run_node_hooks(self.compiled.after_model)
        except BaseException as exc:
            await self.emit_failure(exc, stage="after_model")
            raise

    async def emit_failure(
        self,
        error: BaseException,
        *,
        stage: str,
        detail: Mapping[str, Any] | None = None,
        operation_id: str = "",
        parent_operation_id: str = "",
    ) -> None:
        await self.observers.emit(
            OperationFailedEvent(
                self.context,
                AgentErrorPayload(
                    error=error,
                    stage=stage,
                    detail=detail or {},
                    operation_id=operation_id,
                    parent_operation_id=parent_operation_id,
                ),
            )
        )

    async def run_tool(
        self,
        request: ToolCallRequest,
        *,
        preflight: ToolPreflight,
        execute: ToolExecutor,
    ) -> ToolCallResult:
        """Run wrappers around a sealed preflight → observe → execute terminal."""

        failed_exceptions: set[int] = set()

        async def sealed(current: ToolCallRequest) -> ToolCallResult:
            # Every middleware override and retry re-enters this gate. No wrapper
            # receives the raw executor, so it cannot bypass permission checks.
            try:
                await preflight(current)
            except BaseException as exc:
                failed_exceptions.add(id(exc))
                await self.emit_failure(
                    exc,
                    stage="tool_preflight",
                    detail={
                        "toolName": current.canonical_name,
                        "source": current.source,
                        "serverId": current.server_id,
                        "callId": current.call_id,
                    },
                    parent_operation_id=self.context.agent_execution_id,
                )
                raise
            attempt = self._tool_attempts.get(current.call_id, 0)
            self._tool_attempts[current.call_id] = attempt + 1
            operation_id = str(uuid.uuid4())
            started_payload = ToolCallPayload(
                iteration=current.iteration,
                name=current.canonical_name,
                arguments=current.arguments,
                source=current.source,
                server_id=current.server_id,
                call_id=current.call_id,
                operation_id=operation_id,
                attempt=attempt,
            )
            await self.observers.emit(ToolStartedEvent(self.context, started_payload))
            started_at = time.perf_counter()
            try:
                output = await execute(current)
            except BaseException as exc:
                failed_exceptions.add(id(exc))
                await self.emit_failure(
                    exc,
                    stage="tool_run",
                    detail={
                        "toolName": current.canonical_name,
                        "source": current.source,
                        "serverId": current.server_id,
                        "callId": current.call_id,
                        "attempt": attempt,
                    },
                    operation_id=operation_id,
                    parent_operation_id=self.context.agent_execution_id,
                )
                raise
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            result_payload = ToolResultPayload(
                iteration=current.iteration,
                name=current.canonical_name,
                arguments=current.arguments,
                output=output,
                source=current.source,
                elapsed_ms=elapsed_ms,
                server_id=current.server_id,
                call_id=current.call_id,
                operation_id=operation_id,
                attempt=attempt,
            )
            await self.observers.emit(ToolCompletedEvent(self.context, result_payload))
            return ToolCallResult(
                request=current,
                output=output,
                elapsed_ms=elapsed_ms,
                operation_id=operation_id,
                attempt=attempt,
            )

        handler: AsyncToolCallHandler = sealed
        for wrapper in reversed(self.compiled.wrap_tool):
            inner = handler

            async def composed(
                current: ToolCallRequest,
                *,
                _wrapper: ToolCallMiddleware = wrapper,
                _inner: AsyncToolCallHandler = inner,
            ) -> ToolCallResult:
                return await _wrapper(current, _inner)

            handler = composed
        try:
            return await handler(request)
        except BaseException as exc:
            # Sealed preflight/execute failures already emitted a precise event;
            # only wrapper-originated failures need a middleware-level event.
            if id(exc) not in failed_exceptions:
                await self.emit_failure(
                    exc,
                    stage="tool_middleware",
                    detail={"toolName": request.canonical_name, "callId": request.call_id},
                )
            raise

    async def _run_node_hooks(self, hooks: tuple[NodeHook, ...]) -> None:
        for hook in hooks:
            update = await hook(self.state, self)
            if update:
                self.state.update(update)


class _AgentRunScope(AbstractAsyncContextManager["_AgentRunScope"]):
    """Pair Agent start/completion/failure even across async-generator exits."""

    def __init__(self, runtime: AgentPipelineRuntime, messages: list[ChatMessage]) -> None:
        self._runtime = runtime
        self._messages = tuple(messages)
        self._result: Mapping[str, Any] | None = None
        self._exited = False

    async def __aenter__(self) -> "_AgentRunScope":
        try:
            await self._runtime._run_node_hooks(self._runtime.compiled.before_agent)
        except BaseException as exc:
            await self._runtime.emit_failure(exc, stage="before_agent")
            self._exited = True
            raise
        await self._runtime.observers.emit(
            AgentStartedEvent(self._runtime.context, self._messages)
        )
        return self

    def complete(self, result: Mapping[str, Any]) -> None:
        self._result = result

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        if self._exited:
            return False
        self._exited = True
        if exc is not None:
            await self._runtime.emit_failure(exc, stage="agent_run")
            return False
        if self._result is None:
            error = RuntimeError("Agent run scope exited without a final result")
            await self._runtime.emit_failure(error, stage="agent_run")
            raise error
        await self._runtime.observers.emit(
            AgentCompletedEvent(self._runtime.context, self._result)
        )
        try:
            await self._runtime._run_node_hooks(self._runtime.compiled.after_agent)
        except BaseException as after_exc:
            await self._runtime.emit_failure(after_exc, stage="after_agent")
            raise
        return False
