"""Readable, content-safe terminal logging for the Agent Backend process."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Any, TextIO


LOGGER_NAME = "k_agent.agent_backend"
_HANDLER_MARKER = "_k_agent_backend_handler"


_COMPONENTS = {
    "service": "AgentBackend",
    "agent.request": "AgentRequest",
    "agent.context": "AgentContext",
    "agent.run": "AgentRun",
    "agent.stream": "AgentStream",
    "prompt.compose": "PromptComposer",
    "context.plan": "ContextManager",
    "context.tool_outputs": "ContextManager",
    "model.call": "ModelCall",
    "tool.call": "ToolCall",
    "mcp": "McpRuntime",
}


class AgentBackendTerminalFormatter(logging.Formatter):
    """Render grep-friendly lines with request correlation and compact fields."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        event = str(getattr(record, "event_name", record.getMessage()))
        fields = getattr(record, "event_fields", None)
        fields = dict(fields) if isinstance(fields, dict) else {}
        session_id = _pop_first(fields, "threadId", "sessionId") or "-"
        run_id = _pop_first(fields, "runId", "agentRunId") or "-"
        trace_id = _pop_first(fields, "requestId", "traceId") or "-"
        # agentRunId is an internal loop identifier; retain it as a detail when
        # an external runId already occupies the correlation header.
        component = _component_for(event)
        message = event.replace(".", " ")
        line = (
            f"{timestamp} [{record.levelname}] {record.name} "
            f"[sess={session_id} run={run_id} trace={trace_id}] "
            f"[{component}] {message}"
        )
        for key, value in fields.items():
            if value is not None:
                line += f" | {key}={_format_value(value)}"
        return line


def configure_agent_backend_logging(
    level: str,
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure a process-local logger without changing Uvicorn's own handlers."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False

    # App factories may run repeatedly under reload or tests. Reuse the owned
    # handler so one event is never printed multiple times in the same process.
    handlers = [
        handler
        for handler in logger.handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]
    target_stream = stream or sys.stdout
    handler = handlers[0] if handlers else logging.StreamHandler(target_stream)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(AgentBackendTerminalFormatter())
    handler.setLevel(logging.NOTSET)
    if not handlers:
        logger.addHandler(handler)
    else:
        handler.setStream(target_stream)
    return logger


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event whose callers have already reduced sensitive data."""

    logging.getLogger(LOGGER_NAME).log(
        level,
        event,
        extra={
            "event_name": event,
            "event_fields": fields,
        },
    )


def _coerce_level(value: str) -> int:
    """Normalize an environment-provided log level with an INFO fallback."""

    level = getattr(logging, str(value).strip().upper(), None)
    return level if isinstance(level, int) else logging.INFO


def _component_for(event: str) -> str:
    """Map an event namespace to the stable component shown in terminal logs."""

    for prefix, component in _COMPONENTS.items():
        if event == prefix or event.startswith(f"{prefix}."):
            return component
    return "AgentBackend"


def _pop_first(fields: dict[str, Any], *keys: str) -> Any:
    """Remove and return the first populated correlation field."""

    for key in keys:
        value = fields.pop(key, None)
        if value not in (None, ""):
            return value
    return None


def _format_value(value: Any) -> str:
    """Keep every field on one terminal line without exposing extra structure."""

    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple, set)):
        return "[" + ",".join(_format_value(item) for item in value) + "]"
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return text
