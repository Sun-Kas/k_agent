"""Local operational logging callback for Agent, model, and tool lifecycles."""

from __future__ import annotations

import logging
import time
from typing import Any

from backend.agent.callbacks import (
    AgentErrorPayload,
    AgentRunContext,
    ContextPlanPayload,
    ContextPrunePayload,
    ModelCallPayload,
    ModelResultPayload,
    ToolCallPayload,
    ToolResultPayload,
)
from backend.api.schemas import ChatMessage
from backend.logging_config import log_event


class AgentBackendLoggingCallback:
    """Log lifecycle metadata while excluding prompts, arguments, and outputs."""

    def __init__(self, *, request_id: str, thread_id: str, run_id: str) -> None:
        self._identity = {
            "requestId": request_id or "-",
            "threadId": thread_id,
            "runId": run_id,
        }

    async def on_agent_start(
        self,
        context: AgentRunContext,
        messages: list[ChatMessage],
    ) -> None:
        log_event(
            "agent.run.started",
            **self._identity,
            agentRunId=context.run_id,
            messageCount=len(messages),
        )

    async def before_model(
        self,
        context: AgentRunContext,
        payload: ModelCallPayload,
    ) -> None:
        log_event(
            "model.call.started",
            **self._identity,
            agentRunId=context.run_id,
            iteration=payload.iteration,
            model=payload.model,
            messageCount=len(payload.messages),
            toolDefinitionCount=len(payload.tools),
        )

    async def on_context_built(
        self,
        context: AgentRunContext,
        payload: ContextPlanPayload,
    ) -> None:
        log_event(
            "context.plan.completed",
            **self._identity,
            agentRunId=context.run_id,
            inputMessageCount=payload.input_message_count,
            activeMessageCount=payload.active_message_count,
            providerMessageCount=payload.provider_message_count,
            compactedMessageCount=payload.compacted_message_count,
            summaryChars=payload.summary_chars,
            attachmentCount=payload.attachment_count,
            autoCompacted=payload.auto_compacted,
            contextWindow=payload.budget.get("context_window"),
            inputBudget=payload.budget.get("input_budget"),
            estimatedInput=payload.breakdown.get("estimatedInput"),
            remainingTokens=payload.breakdown.get("remaining"),
            systemTokens=payload.breakdown.get("system"),
            memoryTokens=payload.breakdown.get("memory"),
            skillsAndToolsTokens=payload.breakdown.get("skillsAndTools"),
            messageTokens=payload.breakdown.get("messages"),
            summaryTokens=payload.breakdown.get("summary"),
        )

    async def on_context_pruned(
        self,
        context: AgentRunContext,
        payload: ContextPrunePayload,
    ) -> None:
        log_event(
            "context.tool_outputs.pruned",
            **self._identity,
            agentRunId=context.run_id,
            iteration=payload.iteration,
            prunedOutputCount=payload.pruned_output_count,
            beforeChars=payload.before_chars,
            afterChars=payload.after_chars,
        )

    async def after_model(
        self,
        context: AgentRunContext,
        payload: ModelResultPayload,
    ) -> None:
        log_event(
            "model.call.completed",
            **self._identity,
            agentRunId=context.run_id,
            iteration=payload.iteration,
            model=payload.model,
            responseId=payload.response_id or None,
            elapsedMs=round(payload.elapsed_ms, 3),
            outputChars=len(payload.output_text),
            toolCallCount=payload.function_call_count,
        )

    async def before_tool(
        self,
        context: AgentRunContext,
        payload: ToolCallPayload,
    ) -> None:
        log_event(
            "tool.call.started",
            **self._identity,
            agentRunId=context.run_id,
            iteration=payload.iteration,
            source=payload.source,
            serverId=payload.server_id,
            tool=payload.name,
            argumentKeys=sorted(str(key) for key in payload.arguments),
        )

    async def after_tool(
        self,
        context: AgentRunContext,
        payload: ToolResultPayload,
    ) -> None:
        log_event(
            "tool.call.completed",
            **self._identity,
            agentRunId=context.run_id,
            iteration=payload.iteration,
            source=payload.source,
            serverId=payload.server_id,
            tool=payload.name,
            elapsedMs=round(payload.elapsed_ms, 3),
            outputChars=len(payload.output),
        )

    async def on_error(
        self,
        context: AgentRunContext,
        payload: AgentErrorPayload,
    ) -> None:
        # Exception text may contain provider payloads or user-controlled values.
        # Keep the local error event useful and safe by logging only its class.
        log_event(
            "agent.run.failed",
            level=logging.ERROR,
            **self._identity,
            agentRunId=context.run_id,
            stage=payload.stage,
            errorType=type(payload.error).__name__,
        )

    async def on_agent_end(
        self,
        context: AgentRunContext,
        result: dict[str, Any],
    ) -> None:
        log_event(
            "agent.run.completed",
            **self._identity,
            agentRunId=context.run_id,
            elapsedMs=round(max(0.0, time.time() - context.started_at) * 1000, 3),
            messageCount=len(result.get("messages") or []),
        )
