"""Local fail-open observer for Agent, model, context, and tool lifecycles."""

from __future__ import annotations

import logging
import time

from backend.agent.hooks import (
    AgentCompletedEvent,
    AgentEvent,
    AgentStartedEvent,
    ContextBuiltEvent,
    ContextPrunedEvent,
    ModelCompletedEvent,
    ModelStartedEvent,
    OperationFailedEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from backend.logging_config import log_event


class AgentBackendLoggingObserver:
    """Log lifecycle metadata while excluding prompts, arguments, and outputs."""

    # Local logs intentionally contain only correlation IDs, counts, names,
    # lengths, and timings. Langfuse owns the separately-redacted content path.
    def __init__(self, *, request_id: str, thread_id: str, run_id: str) -> None:
        self._identity = {
            "requestId": request_id or "-",
            "threadId": thread_id,
            "runId": run_id,
        }

    async def handle(self, event: AgentEvent) -> None:
        """Translate typed observer events into the existing process log schema."""

        context = event.context
        if isinstance(event, AgentStartedEvent):
            log_event(
                "agent.run.started", **self._identity,
                agentExecutionId=context.agent_execution_id,
                messageCount=len(event.messages),
            )
        elif isinstance(event, ContextBuiltEvent):
            payload = event.payload
            log_event(
                "context.plan.completed", **self._identity,
                agentExecutionId=context.agent_execution_id,
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
        elif isinstance(event, ContextPrunedEvent):
            payload = event.payload
            log_event(
                "context.tool_outputs.pruned", **self._identity,
                agentExecutionId=context.agent_execution_id,
                iteration=payload.iteration,
                prunedOutputCount=payload.pruned_output_count,
                beforeChars=payload.before_chars,
                afterChars=payload.after_chars,
            )
        elif isinstance(event, ModelStartedEvent):
            payload = event.payload
            log_event(
                "model.call.started", **self._identity,
                agentExecutionId=context.agent_execution_id,
                operationId=payload.operation_id,
                iteration=payload.iteration,
                model=payload.model,
                messageCount=len(payload.messages),
                toolDefinitionCount=len(payload.tools),
            )
        elif isinstance(event, ModelCompletedEvent):
            payload = event.payload
            log_event(
                "model.call.completed", **self._identity,
                agentExecutionId=context.agent_execution_id,
                operationId=payload.operation_id,
                iteration=payload.iteration,
                model=payload.model,
                responseId=payload.response_id or None,
                elapsedMs=round(payload.elapsed_ms, 3),
                outputChars=len(payload.output_text),
                toolCallCount=payload.function_call_count,
            )
        elif isinstance(event, ToolStartedEvent):
            payload = event.payload
            log_event(
                "tool.call.started", **self._identity,
                agentExecutionId=context.agent_execution_id,
                operationId=payload.operation_id,
                callId=payload.call_id,
                attempt=payload.attempt,
                iteration=payload.iteration,
                source=payload.source,
                serverId=payload.server_id,
                tool=payload.name,
                argumentKeys=sorted(str(key) for key in payload.arguments),
            )
        elif isinstance(event, ToolCompletedEvent):
            payload = event.payload
            log_event(
                "tool.call.completed", **self._identity,
                agentExecutionId=context.agent_execution_id,
                operationId=payload.operation_id,
                callId=payload.call_id,
                attempt=payload.attempt,
                iteration=payload.iteration,
                source=payload.source,
                serverId=payload.server_id,
                tool=payload.name,
                elapsedMs=round(payload.elapsed_ms, 3),
                outputChars=len(payload.output),
            )
        elif isinstance(event, OperationFailedEvent):
            payload = event.payload
            if payload.stage.startswith("tool_"):
                detail = payload.detail
                log_event(
                    "tool.call.failed", level=logging.ERROR, **self._identity,
                    agentExecutionId=context.agent_execution_id,
                    operationId=payload.operation_id,
                    source=detail.get("source"),
                    serverId=detail.get("serverId"),
                    tool=detail.get("toolName"),
                    callId=detail.get("callId"),
                    errorType=type(payload.error).__name__,
                )
                return
            if payload.stage in {"model_call", "model_middleware", "before_model", "after_model"}:
                log_event(
                    "model.call.failed", level=logging.ERROR, **self._identity,
                    agentExecutionId=context.agent_execution_id,
                    operationId=payload.operation_id,
                    errorType=type(payload.error).__name__,
                )
                return
            log_event(
                "agent.run.failed", level=logging.ERROR, **self._identity,
                agentExecutionId=context.agent_execution_id,
                operationId=payload.operation_id,
                stage=payload.stage,
                errorType=type(payload.error).__name__,
            )
        elif isinstance(event, AgentCompletedEvent):
            log_event(
                "agent.run.completed", **self._identity,
                agentExecutionId=context.agent_execution_id,
                elapsedMs=round(max(0.0, time.time() - context.started_at) * 1000, 3),
                outputChars=len(str(event.result.get("output") or "")),
            )
