from __future__ import annotations

import io
import logging
import unittest
from datetime import datetime, timezone

from backend.agent.hooks import (
    AgentStartedEvent,
    AgentErrorPayload,
    AgentRunContext,
    ContextBuiltEvent,
    ContextPlanPayload,
    ContextPrunedEvent,
    ContextPrunePayload,
    ModelCallPayload,
    ModelStartedEvent,
    OperationFailedEvent,
    ToolCallPayload,
    ToolStartedEvent,
)
from backend.api.schemas import ChatMessage
from backend.logging_config import configure_agent_backend_logging
from backend.mcp_tool.client import McpClientManager, McpServerConfig
from backend.observability.logging import AgentBackendLoggingObserver


class AgentBackendLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.output = io.StringIO()
        configure_agent_backend_logging("INFO", stream=self.output)
        self.observer = AgentBackendLoggingObserver(
            request_id="request-1",
            thread_id="thread-1",
            run_id="run-1",
        )
        self.context = AgentRunContext(run_id="internal-1")

    async def test_lifecycle_logs_process_summary_without_content_values(self) -> None:
        message = ChatMessage(
            id="message-1",
            role="user",
            content="TOP SECRET USER CONTENT",
            createdAt=datetime.now(timezone.utc),
        )
        await self.observer.handle(AgentStartedEvent(self.context, (message,)))
        await self.observer.handle(
            ModelStartedEvent(
                self.context,
            ModelCallPayload(
                iteration=0,
                model="test-model",
                    messages=({"role": "user", "content": message.content},),
                    tools=(),
                    operation_id="model-op",
                ),
            ),
        )
        await self.observer.handle(
            ToolStartedEvent(
                self.context,
                ToolCallPayload(
                iteration=0,
                name="lookup",
                arguments={"token": "TOP SECRET TOOL ARGUMENT"},
                source="local",
                    operation_id="tool-op",
                    call_id="call-1",
                ),
            ),
        )

        lines = self.output.getvalue().splitlines()
        serialized = "\n".join(lines)
        self.assertEqual(len(lines), 3)
        self.assertIn("sess=thread-1", lines[0])
        self.assertIn("run=run-1", lines[0])
        self.assertIn("Agent 循环开始", lines[0])
        self.assertIn("模型#1 开始", lines[1])
        self.assertIn("test-model", lines[1])
        self.assertIn("工具 lookup 开始", lines[2])
        self.assertNotIn("argumentKeys", lines[2])
        self.assertNotIn("TOP SECRET", serialized)

    async def test_error_logs_type_without_exception_message(self) -> None:
        await self.observer.handle(
            OperationFailedEvent(
                self.context,
                AgentErrorPayload(
                error=RuntimeError("secret provider payload"),
                stage="agent_run",
                ),
            ),
        )

        line = self.output.getvalue()
        self.assertIn("ERROR", line)
        self.assertIn("Agent 失败", line)
        self.assertIn("error=RuntimeError", line)
        self.assertNotIn("secret provider payload", line)

    async def test_context_logs_budget_and_pruning_without_summary_content(self) -> None:
        await self.observer.handle(
            ContextBuiltEvent(
                self.context,
                ContextPlanPayload(
                input_message_count=12,
                active_message_count=6,
                provider_message_count=8,
                compacted_message_count=6,
                summary_chars=420,
                attachment_count=1,
                auto_compacted=True,
                budget={
                    "context_window": 100_000,
                    "max_output_tokens": 8_000,
                    "safety_tokens": 10_000,
                    "input_budget": 82_000,
                },
                breakdown={
                    "system": 100,
                    "memory": 200,
                    "skillsAndTools": 300,
                    "summary": 110,
                    "messages": 500,
                    "estimatedInput": 1_210,
                    "remaining": 80_790,
                },
                ),
            )
        )
        await self.observer.handle(
            ContextPrunedEvent(
                self.context,
                ContextPrunePayload(
                iteration=2,
                pruned_output_count=3,
                before_chars=75_000,
                after_chars=12_000,
                ),
            )
        )

        lines = self.output.getvalue().splitlines()
        self.assertIn("上下文规划", lines[0])
        self.assertIn("剩余=80790", lines[0])
        self.assertIn("已自动压缩", lines[0])
        self.assertNotIn("systemTokens=", lines[0])
        self.assertIn("裁剪旧工具输出", lines[1])
        self.assertIn("75000→12000字", lines[1])

    async def test_mcp_load_logs_server_counts_without_connection_values(self) -> None:
        manager = McpClientManager(
            [
                McpServerConfig(
                    id="calendar",
                    scope="local",
                    type="http",
                    command="",
                    args=[],
                    env={"TOKEN": "TOP SECRET MCP TOKEN"},
                    url="https://secret.example.test/mcp",
                    enabled=False,
                )
            ],
            log_context={
                "requestId": "request-1",
                "threadId": "thread-1",
                "runId": "run-1",
            },
        )

        await manager.connect_all()

        lines = self.output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("准备 MCP", lines[0])
        self.assertIn("启用=0", lines[0])
        self.assertIn("MCP 已禁用", lines[1])
        self.assertIn("MCP 就绪", lines[2])
        self.assertNotIn("TOP SECRET", self.output.getvalue())
        self.assertNotIn("secret.example.test", self.output.getvalue())

    def test_configure_reuses_owned_handler(self) -> None:
        logger = configure_agent_backend_logging("DEBUG", stream=self.output)
        configure_agent_backend_logging("INFO", stream=self.output)

        owned = [
            handler
            for handler in logger.handlers
            if getattr(handler, "_k_agent_log_handler", False)
        ]
        self.assertEqual(len(owned), 1)
        self.assertEqual(logger.level, logging.INFO)


if __name__ == "__main__":
    unittest.main()
