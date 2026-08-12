"""Run-scoped live output bridge for long-running local tools."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Callable

ToolOutputSink = Callable[[dict[str, Any]], None]
_tool_output_sink: ContextVar[ToolOutputSink | None] = ContextVar(
    "k_agent_tool_output_sink", default=None
)


def set_tool_output_sink(sink: ToolOutputSink) -> Token[ToolOutputSink | None]:
    """Bind the current tool call to its Agent event queue."""
    return _tool_output_sink.set(sink)


def reset_tool_output_sink(token: Token[ToolOutputSink | None]) -> None:
    _tool_output_sink.reset(token)


def emit_tool_output(*, stream: str, delta: str, **metadata: Any) -> None:
    """Publish output without coupling tool implementations to the Agent loop."""
    sink = _tool_output_sink.get()
    if sink is not None and delta:
        sink({"stream": stream, "delta": delta, **metadata})
