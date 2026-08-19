"""Fail-open Langfuse tracing for Agent, model, and tool lifecycles."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from typing import Any, Iterator

from langfuse import Langfuse, propagate_attributes
from pydantic import BaseModel

from backend.agent.hooks import (
    AgentCompletedEvent,
    AgentErrorPayload,
    AgentEvent,
    AgentRunContext,
    AgentStartedEvent,
    ModelCallPayload,
    ModelCompletedEvent,
    ModelResultPayload,
    ModelStartedEvent,
    OperationFailedEvent,
    ToolCallPayload,
    ToolCompletedEvent,
    ToolResultPayload,
    ToolStartedEvent,
)
from backend.api.schemas import ChatMessage
from backend.config import Settings


LOGGER = logging.getLogger(__name__)
# 与本地日志不同，Langfuse 会收到 prompt、工具参数和输出的完整正文，
# 因此需要按 key 名脱敏。命中这些名字的值一律替换为 [REDACTED]。
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "secret_key",
    "token",
}


class LangfuseAgentObserver:
    """Request observer that creates children below one Langfuse agent span."""

    def __init__(self, root: Any, runtime: "LangfuseRuntime") -> None:
        self._root = root
        self._runtime = runtime
        # 未收尾的子 observation 按 key 暂存，等对应的 after_* 回调来配对结束。
        # 迭代号必须进 key：同一次 run 里同一个工具可能被多轮反复调用。
        self._generations: dict[str, Any] = {}
        self._tools: dict[str, Any] = {}

    async def handle(self, event: AgentEvent) -> None:
        """Route typed events while keeping SDK failures local to this observer."""

        if isinstance(event, AgentStartedEvent):
            await self.on_agent_start(event.context, list(event.messages))
        elif isinstance(event, ModelStartedEvent):
            await self.before_model(event.context, event.payload)
        elif isinstance(event, ModelCompletedEvent):
            await self.after_model(event.context, event.payload)
        elif isinstance(event, ToolStartedEvent):
            await self.before_tool(event.context, event.payload)
        elif isinstance(event, ToolCompletedEvent):
            await self.after_tool(event.context, event.payload)
        elif isinstance(event, OperationFailedEvent):
            await self.on_error(event.context, event.payload)
        elif isinstance(event, AgentCompletedEvent):
            await self.on_agent_end(event.context, dict(event.result))

    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        self._safe_update(
            self._root,
            input=[_json_safe(message) for message in messages],
            metadata={
                "agentRunId": context.run_id,
                "messageCount": len(messages),
            },
        )

    async def before_model(
        self,
        context: AgentRunContext,
        payload: ModelCallPayload,
    ) -> None:
        key = payload.operation_id
        try:
            self._generations[key] = self._root.start_observation(
                as_type="generation",
                name=f"model-{payload.iteration + 1}",
                model=payload.model,
                input=_json_safe(payload.messages),
                metadata={
                    "iteration": payload.iteration,
                    "toolDefinitionCount": len(payload.tools),
                },
            )
        except Exception as exc:  # Langfuse must never break an Agent request.
            self._runtime.record_error(exc)

    async def after_model(
        self,
        context: AgentRunContext,
        payload: ModelResultPayload,
    ) -> None:
        observation = self._generations.pop(
            payload.operation_id,
            None,
        )
        if observation is None:
            return
        self._safe_update(
            observation,
            output={
                "text": payload.output_text,
                "toolCalls": _json_safe(payload.tool_calls),
            },
            metadata={
                "responseId": payload.response_id,
                "functionCallCount": payload.function_call_count,
                "elapsedMs": round(payload.elapsed_ms, 3),
            },
        )
        self._safe_end(observation)

    async def before_tool(
        self,
        context: AgentRunContext,
        payload: ToolCallPayload,
    ) -> None:
        key = payload.operation_id
        try:
            self._tools[key] = self._root.start_observation(
                as_type="tool",
                name=f"{payload.source}:{payload.name}",
                input=_json_safe(payload.arguments),
                metadata={
                    "iteration": payload.iteration,
                    "source": payload.source,
                    "serverId": payload.server_id,
                },
            )
        except Exception as exc:
            self._runtime.record_error(exc)

    async def after_tool(
        self,
        context: AgentRunContext,
        payload: ToolResultPayload,
    ) -> None:
        observation = self._tools.pop(
            payload.operation_id,
            None,
        )
        if observation is None:
            return
        self._safe_update(
            observation,
            output=_json_safe(payload.output),
            metadata={
                "elapsedMs": round(payload.elapsed_ms, 3),
                "serverId": payload.server_id,
            },
        )
        self._safe_end(observation)

    async def on_error(
        self,
        context: AgentRunContext,
        payload: AgentErrorPayload,
    ) -> None:
        error = f"{type(payload.error).__name__}: {payload.error}"
        if payload.operation_id:
            # A recoverable tool failure must close only its own observation.
            # Closing every child here would corrupt parallel same-name calls.
            observation = self._tools.pop(payload.operation_id, None)
            if observation is None:
                observation = self._generations.pop(payload.operation_id, None)
            if observation is not None:
                self._safe_update(
                    observation,
                    level="ERROR",
                    status_message=error,
                    metadata={
                        "agentRunId": context.run_id,
                        "errorStage": payload.stage,
                        "errorDetail": _json_safe(payload.detail),
                    },
                )
                self._safe_end(observation)
            return
        if payload.stage.startswith("tool_"):
            # Resolution, argument, and permission failures happen before a
            # physical tool observation exists and are recoverable by the model.
            # They must not mark the whole Agent root as failed.
            return
        self._safe_update(
            self._root,
            level="ERROR",
            status_message=error,
            metadata={
                "agentRunId": context.run_id,
                "errorStage": payload.stage,
                "errorDetail": _json_safe(payload.detail),
            },
        )
        self._close_open_children(level="ERROR", status_message=error)

    async def on_agent_end(
        self,
        context: AgentRunContext,
        result: dict[str, Any],
    ) -> None:
        messages = result.get("messages") or []
        final_message = next(
            (
                message
                for message in reversed(messages)
                if getattr(message, "role", None) == "assistant"
                or (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                )
            ),
            None,
        )
        self._safe_update(
            self._root,
            output=_json_safe(final_message),
            metadata={
                "agentRunId": context.run_id,
                "messageCount": len(messages),
                "elapsedMs": round(
                    max(0.0, time.time() - context.started_at) * 1000,
                    3,
                ),
            },
        )
        self._close_open_children(
            level="WARNING",
            status_message="Agent ended before child observation completed",
        )

    def _close_open_children(self, *, level: str, status_message: str) -> None:
        # run 结束或出错时兜底收尾：模型流中断、工具抛异常都会让 after_* 回调
        # 没机会执行，留下的 observation 在 Langfuse 上会永远显示为进行中。
        for observation in [*self._generations.values(), *self._tools.values()]:
            self._safe_update(
                observation,
                level=level,
                status_message=status_message,
            )
            self._safe_end(observation)
        self._generations.clear()
        self._tools.clear()

    def _safe_update(self, observation: Any, **values: Any) -> None:
        try:
            observation.update(**_json_safe(values))
        except Exception as exc:
            self._runtime.record_error(exc)

    def _safe_end(self, observation: Any) -> None:
        try:
            observation.end()
        except Exception as exc:
            self._runtime.record_error(exc)


class LangfuseRuntime:
    """Own one process-level Langfuse client and request-scoped trace contexts."""

    def __init__(self, settings: Settings) -> None:
        # configured 与 enabled 分开是为了让 /internal/health 能区分
        # 「没配密钥」和「配了但被开关关掉 / 初始化失败」两种情况。
        self.configured = bool(
            settings.langfuse_public_key and settings.langfuse_secret_key
        )
        self.enabled = bool(settings.langfuse_enabled and self.configured)
        self.authenticated: bool | None = None
        self.last_error: str | None = None
        self._client: Langfuse | None = None
        if self.enabled:
            try:
                self._client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    base_url=settings.langfuse_base_url,
                    timeout=settings.langfuse_timeout_seconds,
                    environment=settings.langfuse_environment,
                    release=settings.langfuse_release,
                    sample_rate=settings.langfuse_sample_rate,
                    mask=_mask_sensitive_data,
                )
            except Exception as exc:
                self.record_error(exc)
                self.enabled = False

    async def startup(self) -> None:
        """Verify credentials without preventing Agent Backend startup."""
        if self._client is None:
            return
        # SDK 是同步阻塞的，放到线程里做，避免启动期占住事件循环。
        try:
            self.authenticated = await asyncio.to_thread(self._client.auth_check)
            if not self.authenticated:
                self.last_error = "Langfuse authentication failed"
        except Exception as exc:
            self.authenticated = False
            self.record_error(exc)

    async def shutdown(self) -> None:
        """Flush buffered observations during process shutdown."""
        if self._client is None:
            return
        # observation 是攒批上报的，不 flush 直接退出会丢掉最后一批 trace。
        try:
            await asyncio.to_thread(self._client.flush)
            await asyncio.to_thread(self._client.shutdown)
        except Exception as exc:
            self.record_error(exc)

    def status(self) -> dict[str, Any]:
        """Return connection feedback without exposing credentials."""
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "authenticated": self.authenticated,
            "lastError": self.last_error,
        }

    @contextmanager
    def observe_agent_run(
        self,
        *,
        session_id: str,
        run_id: str,
        model: str,
        messages: list[ChatMessage],
        metadata: dict[str, Any],
    ) -> Iterator[list[LangfuseAgentObserver]]:
        """Yield a request observer below the root, or none when disabled."""
        if self._client is None:
            yield []
            return

        root_manager = None
        attributes_manager = None
        root = None
        try:
            root_manager = self._client.start_as_current_observation(
                as_type="agent",
                name="k-agent-run",
                input=[_json_safe(message) for message in messages],
                model=model,
                metadata=_json_safe({"runId": run_id, **metadata}),
            )
            root = root_manager.__enter__()
            attributes_manager = propagate_attributes(
                trace_name="k-agent-run",
                session_id=session_id,
                tags=["k-agent", "agent-backend"],
                metadata={"runId": run_id},
            )
            attributes_manager.__enter__()
        except Exception as exc:
            # 建 trace 失败就退化成「无回调」运行：可观测性不可用不应影响出话。
            self.record_error(exc)
            self._exit_manager(attributes_manager)
            self._exit_manager(root_manager)
            yield []
            return

        try:
            yield [LangfuseAgentObserver(root, self)]
        except BaseException:
            # 捕 BaseException 是为了覆盖客户端断开导致的 CancelledError：
            # 这两个上下文管理器必须关掉，否则 trace 一直挂着且不会上报。
            error_info = sys.exc_info()
            self._exit_manager(attributes_manager, error_info)
            self._exit_manager(root_manager, error_info)
            raise
        else:
            self._exit_manager(attributes_manager)
            self._exit_manager(root_manager)

    def record_error(self, error: BaseException) -> None:
        """Record a sanitized SDK error and keep the request path fail-open."""
        # 截断到 500 字符：SDK 异常里可能带上完整请求体，既冗长又可能含敏感内容。
        self.last_error = f"{type(error).__name__}: {error}"[:500]
        LOGGER.warning("Langfuse observability error: %s", self.last_error)

    def _exit_manager(
        self,
        manager: Any,
        error_info: tuple[Any, Any, Any] = (None, None, None),
    ) -> None:
        if manager is None:
            return
        try:
            manager.__exit__(*error_info)
        except Exception as exc:
            self.record_error(exc)


def _json_safe(value: Any) -> Any:
    """Convert runtime objects to bounded, secret-safe Langfuse payloads."""
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(by_alias=True, mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _is_sensitive_key(str(key))
                else _json_safe(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    # 图片 data URL 是 base64 大块内容，上报既无可读性又会撑爆 trace 体积。
    if isinstance(value, str) and value.startswith("data:image/"):
        media_type = value.partition(";")[0].removeprefix("data:")
        return f"[{media_type} data omitted]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _mask_sensitive_data(value: Any = None, **kwargs: Any) -> Any:
    """Mask positional data and the keyword form used by newer Langfuse SDKs."""

    return _json_safe(kwargs.get("data", value))


def _is_sensitive_key(key: str) -> bool:
    # 归一化大小写和连字符，让 API-Key、api_key、apiKey 都能命中同一条规则。
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )
