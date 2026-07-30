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

from backend.agent.callbacks import (
    AgentErrorPayload,
    AgentRunContext,
    ModelCallPayload,
    ModelResultPayload,
    ToolCallPayload,
    ToolResultPayload,
)
from backend.api.schemas import ChatMessage
from backend.config import Settings


LOGGER = logging.getLogger(__name__)
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


class LangfuseAgentCallback:
    """Project Agent callback that creates children below one Langfuse agent span."""

    def __init__(self, root: Any, runtime: "LangfuseRuntime") -> None:
        self._root = root
        self._runtime = runtime
        self._generations: dict[tuple[str, int], Any] = {}
        self._tools: dict[tuple[str, int, str, str], Any] = {}

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
        key = (context.run_id, payload.iteration)
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
            (context.run_id, payload.iteration),
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
        key = (context.run_id, payload.iteration, payload.source, payload.name)
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
            (context.run_id, payload.iteration, payload.source, payload.name),
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
    ) -> Iterator[list[LangfuseAgentCallback]]:
        """Yield a callback below a request root, or no callbacks when disabled."""
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
            self.record_error(exc)
            self._exit_manager(attributes_manager)
            self._exit_manager(root_manager)
            yield []
            return

        try:
            yield [LangfuseAgentCallback(root, self)]
        except BaseException:
            error_info = sys.exc_info()
            self._exit_manager(attributes_manager, error_info)
            self._exit_manager(root_manager, error_info)
            raise
        else:
            self._exit_manager(attributes_manager)
            self._exit_manager(root_manager)

    def record_error(self, error: BaseException) -> None:
        """Record a sanitized SDK error and keep the request path fail-open."""
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
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )
