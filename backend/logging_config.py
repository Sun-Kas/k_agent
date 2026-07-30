"""Readable process logging for K Agent.

Process events use INFO; real failures use ERROR. Correlation IDs are shortened
and INFO lines stay as short Chinese summaries. Extra structured fields only
appear at DEBUG. Third-party libraries stay quiet at WARNING+.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, TextIO


LOGGER_NAME = "k_agent"
_HANDLER_MARKER = "_k_agent_log_handler"

_QUIET_LOGGERS = (
    "mcp",
    "mcp.server",
    "mcp.client",
    "mcp.client.streamable_http",
    "httpx",
    "httpcore",
    "openai",
    "openai._base_client",
    "anyio",
    "asyncio",
    "urllib3",
    "sse_starlette",
    "uvicorn.error",
)


SummaryBuilder = Callable[[dict[str, Any]], str]


def _sec(fields: dict[str, Any]) -> str:
    raw = fields.get("elapsedMs")
    if raw is None:
        return ""
    try:
        return f"{float(raw) / 1000:.1f}s"
    except (TypeError, ValueError):
        return ""


def _iter_label(fields: dict[str, Any]) -> str:
    try:
        return str(int(fields.get("iteration", 0)) + 1)
    except (TypeError, ValueError):
        return "?"


def _tool_label(fields: dict[str, Any]) -> str:
    tool = str(fields.get("tool") or "tool")
    source = fields.get("source")
    server = fields.get("serverId")
    if source == "mcp" and server:
        return f"{server}/{tool}"
    return tool


_SUMMARIES: dict[str, SummaryBuilder] = {
    "service.starting": lambda f: (
        f"Agent Backend 启动 · {f.get('host')}:{f.get('port')}"
    ),
    "service.ready": lambda f: (
        f"Agent Backend 就绪 · MCP已连接={f.get('connectedMcpServerCount', 0)}/"
        f"{f.get('mcpServerCount', 0)}"
    ),
    "service.stopping": lambda _f: "Agent Backend 正在停止",
    "service.stopped": lambda _f: "Agent Backend 已停止",
    "access.run.accepted": lambda f: (
        f"接入层接收用户消息 · 转发历史={f.get('historyCount')} "
        f"MCP={f.get('mcpCount')} Skill={f.get('skillCount')}"
    ),
    "access.run.finished": lambda f: (
        f"接入层转发结束 · {_sec(f) or '完成'}"
    ),
    "access.run.failed": lambda f: (
        f"接入层运行失败 · {f.get('errorType')}"
    ),
    "agent.request.received": lambda f: (
        f"后端收到运行 · 历史消息={f.get('messageCount')} "
        f"MCP={f.get('selectedMcpServerCount')} Skill={f.get('selectedSkillCount')}"
        + (f" 附件={f.get('attachmentCount')}" if f.get("attachmentCount") else "")
    ),
    "mcp.load.started": lambda f: (
        f"准备 MCP · 启用={f.get('enabledServerCount')} 禁用={f.get('disabledServerCount')}"
    ),
    "mcp.load.completed": lambda f: (
        f"MCP 就绪 · 已连接={f.get('connectedServerCount')}"
        + (f" 失败={f.get('failedServerCount')}" if f.get("failedServerCount") else "")
    ),
    "mcp.reload.started": lambda _f: "正在重新加载 MCP",
    "mcp.reload.completed": lambda f: (
        f"MCP 重载完成 · 服务={f.get('mcpServerCount')} 工具={f.get('mcpToolCount')}"
    ),
    "mcp.server.disabled": lambda f: f"MCP 已禁用 · {f.get('serverId')}",
    "mcp.server.connect.started": lambda f: f"MCP 连接中 · {f.get('serverId')}",
    "mcp.server.connect.completed": lambda f: (
        f"MCP 已连接 · {f.get('serverId')} {_sec(f)}"
    ).strip(),
    "mcp.server.connect.failed": lambda f: (
        f"MCP 连接失败 · {f.get('serverId')} ({f.get('errorType')}"
        + (f": {f.get('errorDetail')}" if f.get("errorDetail") else "")
        + ")"
    ),
    "mcp.tools.loaded": lambda f: f"MCP 工具已加载 · {f.get('toolCount')} 个",
    "prompt.compose.started": lambda f: (
        f"开始组装提示词 · skill={f.get('selectedSkillCount')} mcp工具={f.get('mcpToolCount')}"
    ),
    "mcp.server.stderr": lambda f: f"MCP[{f.get('serverId')}] {f.get('line')}",
    "mcp.server.stderr.suppressed": lambda f: (
        f"MCP[{f.get('serverId')}] 已折叠噪声日志 {f.get('suppressedLines')} 行"
    ),
    "prompt.compose.completed": lambda f: (
        f"提示词就绪 · system={f.get('systemPromptChars')}字 "
        f"memory={f.get('memoryFileCount')} skill={f.get('selectedSkillCount')} "
        f"mcp指令={f.get('mcpInstructionCount')}"
    ),
    "agent.context.prepared": lambda f: (
        f"运行上下文就绪 · 模型={f.get('model')} 工具={f.get('localAndSelectedToolCount')}"
    ),
    "agent.run.started": lambda f: f"Agent 循环开始 · 上下文消息={f.get('messageCount')}",
    "agent.run.completed": lambda f: f"Agent 循环结束 · {_sec(f)}",
    "agent.run.failed": lambda f: (
        f"Agent 失败 · stage={f.get('stage')} error={f.get('errorType')}"
    ),
    "agent.stream.closed": lambda f: f"事件流关闭 · {_sec(f)}",
    "agent.stream.cancelled": lambda f: f"事件流取消 · {_sec(f)}",
    "agent.stream.failed": lambda f: (
        f"事件流失败 · {f.get('errorType')} {_sec(f)}"
    ).strip(),
    "model.call.started": lambda f: (
        f"模型#{_iter_label(f)} 开始 · {f.get('model')}"
    ),
    "model.call.completed": lambda f: (
        f"模型#{_iter_label(f)} 完成 · {_sec(f)} → "
        + (
            f"调用工具×{f.get('toolCallCount')}"
            if int(f.get("toolCallCount") or 0) > 0
            else f"文本 {f.get('outputChars')}字"
        )
    ),
    "tool.call.started": lambda f: f"工具 {_tool_label(f)} 开始",
    "tool.call.completed": lambda f: (
        f"工具 {_tool_label(f)} 完成 · {_sec(f)} 输出={f.get('outputChars')}字"
    ),
    "context.plan.completed": lambda f: (
        f"上下文规划 · 输入≈{f.get('estimatedInput')}token "
        f"剩余={f.get('remainingTokens')}"
        + (" · 已自动压缩" if f.get("autoCompacted") else "")
    ),
    "context.tool_outputs.pruned": lambda f: (
        f"裁剪旧工具输出 · #{_iter_label(f)} "
        f"{f.get('beforeChars')}→{f.get('afterChars')}字"
    ),
}


class ProcessLogFormatter(logging.Formatter):
    """One-line process narrative with short correlation ids."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        event = str(getattr(record, "event_name", "") or "")
        fields = getattr(record, "event_fields", None)
        fields = dict(fields) if isinstance(fields, dict) else {}

        session_id = _short_id(_pop_first(fields, "threadId", "sessionId"))
        run_id = _short_id(_pop_first(fields, "runId", "agentRunId"))
        fields.pop("agentRunId", None)
        fields.pop("runId", None)
        fields.pop("requestId", None)
        fields.pop("traceId", None)

        summary = fields.pop("summary", None)
        if not summary:
            builder = _SUMMARIES.get(event)
            summary = builder(fields) if builder else (event or record.getMessage())

        prefix = f"{timestamp} {record.levelname:<5}"
        if session_id != "-" or run_id != "-":
            prefix += f" sess={session_id} run={run_id}"
        line = f"{prefix} │ {summary}"

        # DEBUG keeps leftover structured fields for deep inspection.
        if record.levelno <= logging.DEBUG:
            extras = [
                f"{key}={_format_value(value)}"
                for key, value in fields.items()
                if value is not None and key != "line"
            ]
            if extras:
                line += " · " + " ".join(extras)
        return line


class _HealthAccessFilter(logging.Filter):
    """Drop routine health/catalog probes from Uvicorn access logs."""

    _SKIP_FRAGMENTS = (
        "/internal/health",
        "/api/health",
        "/api/catalog",
        '"GET /api/sessions HTTP',
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(fragment in message for fragment in self._SKIP_FRAGMENTS)


def configure_agent_backend_logging(
    level: str,
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure process logging for Access Layer and Agent Backend."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False

    handlers = [
        handler
        for handler in logger.handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]
    target_stream = stream or sys.stdout
    handler = handlers[0] if handlers else logging.StreamHandler(target_stream)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(ProcessLogFormatter())
    handler.setLevel(logging.NOTSET)
    if not handlers:
        logger.addHandler(handler)
    else:
        handler.setStream(target_stream)

    # Compatibility alias used by older imports/tests.
    logging.getLogger("k_agent.agent_backend").handlers = []
    logging.getLogger("k_agent.agent_backend").propagate = True
    logging.getLogger("k_agent.agent_backend").setLevel(logging.NOTSET)

    _quiet_third_party_loggers()
    _configure_uvicorn_access_filter()
    return logger


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event. Prefer known event names so INFO stays narrative."""

    logging.getLogger(LOGGER_NAME).log(
        level,
        event,
        extra={
            "event_name": event,
            "event_fields": fields,
        },
    )


def _quiet_third_party_loggers() -> None:
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _configure_uvicorn_access_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if any(isinstance(item, _HealthAccessFilter) for item in access.filters):
        return
    access.addFilter(_HealthAccessFilter())


def _coerce_level(value: str) -> int:
    level = getattr(logging, str(value).strip().upper(), None)
    return level if isinstance(level, int) else logging.INFO


def _pop_first(fields: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = fields.pop(key, None)
        if value not in (None, ""):
            return value
    return None


def _short_id(value: Any, *, size: int = 8) -> str:
    if value in (None, "", "-"):
        return "-"
    text = str(value)
    return text if len(text) <= size else text[:size]


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple, set)):
        return "[" + ",".join(_format_value(item) for item in value) + "]"
    return str(value).replace("\r", "\\r").replace("\n", "\\n")
