"""把声明式 Hook 编译成蓝图，再绑定到一次请求级 Runtime。

生命周期：
1. 进程启动时 ``AgentPipelineDefinition.compile(middleware=...)``
   —— 不可变，不含 ``run_id`` / messages。``KAgentRunner`` 构造时编译一次，
   多轮对话复用同一份。
2. 每次 K Agent run：``definition.bind_runtime(context, observers)``
   —— 得到 ``AgentPipelineRuntime``。并发两个会话不会共用 context / state /
   observer 实例。

ReAct 循环（``OpenAIAgent.run_stream_react``）只通过本 Runtime 接触三块执行：
- ``agent_run(...)``：配对 before_agent / AgentStarted 与 after_agent / 完成或失败
- ``stream_model(...)``：before_model → wrap_model 洋葱 → sealed 提供商流
- ``run_tool(...)``：wrap_tool 洋葱 → **焊死的** preflight → 观测 → execute

权限、Skill 白名单、参数 schema **不是** 列表里的 Middleware，它们在
``run_tool`` 的 sealed 终端里。列表中的 wrap 既拆不掉也绕不过。
"""

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


# preflight：权限 / HITL / schema 等；execute：真正跑 Local 或 MCP。
# 二者由 react_agent 注入，Runtime 只保证「每次尝试都先 preflight」。
ToolPreflight = Callable[[ToolCallRequest], Awaitable[None]]
ToolExecutor = Callable[[ToolCallRequest], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class _CompiledHooks:
    """排好序的函数引用。after_* 已在编译期逆序，wrap 仍按 order 升序存放，
    运行时再 ``reversed`` 套洋葱（小 order 在最外层）。"""
    
    '''
    `_CompiledHooks` 是一份**已经排好序、不可变的 Hook 函数清单**。进程启动时 `AgentPipelineDefinition.compile()` 把声明式 Middleware 分拣、排序后塞进这里；之后每次 run 的 `AgentPipelineRuntime` 只拿这份引用去执行，不再重新扫描列表。

    六个字段对应 ReAct 循环里三类扩展点：

    | 字段 | 何时跑 | 干什么 |
    | --- | --- | --- |
    | `before_agent` / `after_agent` | 整次 Agent 进出 | 进入前准备、退出后收尾 |
    | `before_model` / `after_model` | 每次模型调用进出 | 改 `state["model_request"]`、看结果 |
    | `wrap_tool` / `wrap_model` | 包在真正调用外面 | 洋葱层：改请求、短路、重试 |

    `frozen=True, slots=True` 表示编译结果不能改、字段固定，多轮对话可以安全共用同一份。

    顺序有意做成两套：

    - **`after_*` 在编译期就逆序**（见 `compile()` 里 `reverse=True`），这样 `before` 按 `order` 从小到大进入，`after` 按相反顺序退出，成对展开/收尾。
    - **`wrap_*` 仍按 `order` 升序存放**，运行时再 `reversed(...)` 从内往外套：小 `order` 在最外层，大 `order` 更靠近真正的模型/工具调用。

    它本身不执行任何逻辑，只是蓝图里的「排好队的函数指针」。真正调用发生在 `_run_node_hooks`、`stream_model`、`run_tool`。Observer 不进这个结构，必须在 `bind_runtime` 时单独注入。
    '''


    before_agent: tuple[NodeHook, ...] = ()
    after_agent: tuple[NodeHook, ...] = ()
    before_model: tuple[NodeHook, ...] = ()
    after_model: tuple[NodeHook, ...] = ()
    wrap_tool: tuple[ToolCallMiddleware, ...] = ()
    wrap_model: tuple[ModelCallMiddleware, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentPipelineDefinition:
    """进程级、无请求数据的 Hook 蓝图。"""

    compiled: _CompiledHooks = field(default_factory=_CompiledHooks)

    @classmethod
    def compile(cls, middleware: list[Any] | tuple[Any, ...] = ()) -> "AgentPipelineDefinition":
        """校验显式 Middleware，并编译出确定性执行顺序。

        - 同 ``order`` 时用列表下标做稳定次序
        - ``after_*`` 逆序，便于与 before 成对展开/收尾
        - Observer 出现在 middleware 列表里直接报错（必须走 bind_runtime）
        """

        grouped: dict[HookKind, list[tuple[int, int, Any]]] = {
            kind: [] for kind in HookKind if kind is not HookKind.OBSERVER
        }
        names: set[str] = set()
        for index, extension in enumerate(middleware):
            # 获取装饰器元数据
            spec = hook_spec(extension)
            if spec is None or spec.kind is HookKind.OBSERVER:
                raise TypeError("Middleware must use a before/after/wrap decorator")
            if spec.name in names:
                raise ValueError(f"Duplicate middleware name: {spec.name}")
            # 添加到名称集合中，确保没有重复的名称
            names.add(spec.name)
            # 按 kind 分组，每个组内按 order 排序
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
        """为这一次 run 创建隔离的 state 与请求级 Observer。"""

        return AgentPipelineRuntime(
            context=context,
            observers=ObserverDispatcher(observers),
            compiled=self.compiled,
        )


@dataclass(slots=True)
class AgentPipelineRuntime:
    """单次 run 的 Observer 派发器、Middleware 黑板和工具 attempt 计数。

    ``state`` / ``request_state`` / ``_tool_attempts`` 都绑在这份 Runtime 上。
    同一 ``call_id`` 的重试会递增 attempt，并分配新的 ``operation_id``。
    """

    context: AgentRunContext
    observers: ObserverDispatcher
    compiled: _CompiledHooks
    state: dict[str, Any] = field(default_factory=dict)
    request_state: dict[str, Any] = field(default_factory=dict)
    _tool_attempts: dict[str, int] = field(default_factory=dict)

    async def emit_context_built(self, payload: ContextPlanPayload) -> None:
        """上下文规划完成后由 ReAct 循环调用；Pipeline 不读消息正文。"""

        await self.observers.emit(ContextBuiltEvent(self.context, payload))

    async def emit_context_pruned(self, payload: ContextPrunePayload) -> None:
        """旧工具输出被裁剪后由 ReAct 循环调用。"""

        await self.observers.emit(ContextPrunedEvent(self.context, payload))

    def agent_run(
        self, messages: list[ChatMessage]
    ) -> "_AgentRunScope":
        """返回异步上下文：保证开始/结束/失败成对，包括异步生成器提前退出。"""

        return _AgentRunScope(self, messages)

    async def stream_model(
        self,
        request: ModelCallPayload,
        terminal: AsyncModelCallHandler,
    ) -> AsyncIterator[ModelStreamEvent]:
        """在保持反压的模型流外包一层 node/wrap Middleware。

        可见的 reasoning/text delta 一旦 yield 给上层（最终到前端），外层
        wrap 就不能再对同一逻辑调用重试，否则用户会看到「半截输出 + 重来」。
        ``failed_exceptions`` 用 ``id(exc)`` 去重，避免同一异常被
        ``OperationFailed`` 报两次。
        """

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
            # 每次真正打提供商前换新 operation_id，便于把重试和首次调用分开观测。
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

        # 把一层 wrap_model 焊到 inner 上：wrapper(request, call_next)。
        # guarded_inner 才是 wrapper 看到的 call_next，用来拦「可见流已经出门后再 retry」。
        def compose_one(
            wrapper: ModelCallMiddleware,
            inner: AsyncModelCallHandler,
        ) -> AsyncModelCallHandler:
            committed = False
            calls = 0

            async def guarded_inner(
                current: ModelCallPayload,
            ) -> AsyncIterator[ModelStreamEvent]:
                # 同一层 wrap 多次调用 inner = 重试。reasoning/text 一旦 yield，
                # 前端已经看到半截字；再打第二枪会叠出重复或错乱输出。
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
                # 把真正的 inner 藏在 guarded_inner 后面，wrapper 不能直接碰到裸 terminal。
                async for item in wrapper(current, guarded_inner):
                    yield item

            return composed

        # 从内到外：先套最里层 wrap（大 order），最后套小 order，使其成为最外层。
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
        """向 Observer 发失败快照；调用方仍须把异常继续抛出去。"""

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
        """工具调用的唯一入口：外层 wrap_tool，内层焊死的 sealed terminal。

        Local / MCP 都走这里，差别只在调用方传入的 ``preflight`` / ``execute``。
        Middleware 无论 override 还是 retry，都只能 ``call_next`` 回到 ``sealed``，
        拿不到裸 ``execute``，因此不能跳过权限、Skill 白名单或参数 schema。
        """

        # 已在 sealed 里报过的异常用 id 去重，避免外层再发一条更糊的 OperationFailed。
        failed_exceptions: set[int] = set()

        async def sealed(current: ToolCallRequest) -> ToolCallResult:
            # wrap 改请求或重试后仍必须回到这里，不能直达 execute。
            try:
                # HITL / 权限发生在这里，此时还没有 ToolStarted，中断通常也没有 live stdout。
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
            # call_id 在多次 retry 间保持；每次进门换新 operation_id，attempt +1。
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

        # 从内到外套洋葱：大 order 更靠近 sealed，小 order 在最外层。
        handler: AsyncToolCallHandler = sealed
        for wrapper in reversed(self.compiled.wrap_tool):
            inner = handler

            # 默认参数绑定当前 wrapper/inner，避免闭包在循环里全部指向最后一项。
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
            # sealed 里的 preflight/execute 已发过更精确的 stage；这里只补 wrap 自己抛的错。
            if id(exc) not in failed_exceptions:
                await self.emit_failure(
                    exc,
                    stage="tool_middleware",
                    detail={"toolName": request.canonical_name, "callId": request.call_id},
                )
            raise

    async def _run_node_hooks(self, hooks: tuple[NodeHook, ...]) -> None:
        """顺序执行 before/after 钩子；返回的 dict 合并进本次 Runtime.state。"""

        for hook in hooks:
            update = await hook(self.state, self)
            if update:
                self.state.update(update)


class _AgentRunScope(AbstractAsyncContextManager["_AgentRunScope"]):
    """配对 Agent 的开始 / 完成 / 失败，覆盖异步生成器中途退出。

    ReAct 循环是 ``async for``，客户端断开时生成器会被 aclose。若不用
    context manager，容易只发出 Started 却漏掉 Failed/Completed。
    ``complete(result)`` 必须在成功路径上调用，否则 ``__aexit__`` 会当成协议错误。
    """

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
        """标记正常收尾结果；真正的 AgentCompleted 在 ``__aexit__`` 里发出。"""

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
