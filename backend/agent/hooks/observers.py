"""Observer 的 fail-open、确定性派发。

Observer 只能看，不能改执行结果。单个 observer 超时或抛错时：
- ``CancelledError`` 继续向上传，取消 run 不能被观测层吞掉
- 其它异常只打 warning（observer 名 + 事件名 + 异常类型），**不**序列化
  event / exception 文本，以免把用户 prompt 或工具正文写进日志
- run 本身继续（fail-open）

排序：``(order, 注册下标)`` 升序，同 order 时保持 ``bind_runtime`` 传入顺序。
带 ``@observe(event_type=...)`` 的函数只收那一类事件；实现了 ``handle`` 的
对象默认收全部事件。
"""

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
    """请求级 Observer 协议：只消费冻结事件，不得回写 Agent。"""

    async def handle(self, event: AgentEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class _ObserverEntry:
    """一次 bind 内排好序的观察者描述；不含事件正文。"""

    observer: Any
    name: str
    order: int
    registration_index: int
    event_type: AgentEventType | None
    timeout_seconds: float | None


class ObserverDispatcher:
    """按固定顺序逐个派发，并把每个 Observer 的失败隔离开。

    故意串行而不是 ``gather``：保证 trace 行序稳定，也避免并发写同一
    ``trace`` 列表。Telemetry 慢不应通过并行来「修」，而应设 ``timeout_seconds``。
    """

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
        """发出一条不可变事件；遥测失败不得打断这次 run。"""

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
                # 这里绝不能把 event 或异常文本打进日志：两者都可能含用户/模型正文。
                LOGGER.warning(
                    "Agent observer failed: observer=%s event=%s error=%s",
                    entry.name,
                    event.type.value,
                    type(exc).__name__,
                )


class TraceObserver:
    """沿用原来的紧凑 trace 行，只记身份/计数/耗时，不观察请求正文。

    ``trace`` 列表由调用方持有（通常是这次 run 的 runtime dict），Observer
    只追加，方便测试和调试面板复用同一表面。
    """

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
