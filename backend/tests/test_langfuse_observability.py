from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from backend.agent.hooks import (
    AgentRunContext,
    ModelCallPayload,
    ModelResultPayload,
    ToolCallPayload,
    ToolResultPayload,
)
from backend.api.schemas import ChatMessage
from backend.observability.langfuse import (
    LangfuseAgentObserver,
    LangfuseRuntime,
    _json_safe,
    _mask_sensitive_data,
)
from backend.config import Settings


class _FakeObservation:
    def __init__(self, **created: Any) -> None:
        self.created = created
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **values: Any) -> None:
        self.updates.append(values)

    def end(self) -> None:
        self.ended = True


class _FakeRoot(_FakeObservation):
    def __init__(self) -> None:
        super().__init__()
        self.children: list[_FakeObservation] = []

    def start_observation(self, **values: Any) -> _FakeObservation:
        child = _FakeObservation(**values)
        self.children.append(child)
        return child


class _FakeRuntime:
    def __init__(self) -> None:
        self.errors: list[BaseException] = []

    def record_error(self, error: BaseException) -> None:
        self.errors.append(error)


class LangfuseObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_records_agent_generation_and_tool_hierarchy(self) -> None:
        root = _FakeRoot()
        runtime = _FakeRuntime()
        observer = LangfuseAgentObserver(root, runtime)  # type: ignore[arg-type]
        context = AgentRunContext(run_id="agent-run")
        message = ChatMessage(
            id="user-1",
            role="user",
            content="hello",
            createdAt=datetime.now(timezone.utc),
        )

        await observer.on_agent_start(context, [message])
        await observer.before_model(
            context,
            ModelCallPayload(
                iteration=0,
                model="test-model",
                messages=({"role": "user", "content": "hello"},),
                tools=(),
                operation_id="model-op",
            ),
        )
        await observer.after_model(
            context,
            ModelResultPayload(
                iteration=0,
                model="test-model",
                response_id="response-1",
                output_text="calling a tool",
                function_call_count=1,
                elapsed_ms=12.5,
                tool_calls=({"name": "lookup", "arguments": {"query": "hello"}},),
                operation_id="model-op",
            ),
        )
        await observer.before_tool(
            context,
            ToolCallPayload(
                iteration=0,
                name="lookup",
                arguments={"query": "hello"},
                source="local",
                call_id="call-lookup",
                operation_id="tool-op",
            ),
        )
        await observer.after_tool(
            context,
            ToolResultPayload(
                iteration=0,
                name="lookup",
                arguments={"query": "hello"},
                output="result",
                source="local",
                elapsed_ms=4.0,
                call_id="call-lookup",
                operation_id="tool-op",
            ),
        )
        await observer.on_agent_end(context, {"output": "assistant output"})

        self.assertEqual(
            [child.created["as_type"] for child in root.children],
            ["generation", "tool"],
        )
        self.assertTrue(all(child.ended for child in root.children))
        self.assertEqual(runtime.errors, [])
        self.assertEqual(
            root.children[0].updates[0]["metadata"]["responseId"],
            "response-1",
        )
        self.assertEqual(root.updates[-1]["output"], "assistant output")
        self.assertEqual(root.updates[-1]["metadata"]["outputChars"], 16)

    def test_sensitive_fields_and_image_payloads_are_masked(self) -> None:
        masked = _json_safe(
            {
                "apiKey": "secret-value",
                "nested": {
                    "authorization": "Bearer token",
                    "image": "data:image/png;base64,AAAA",
                },
            }
        )

        self.assertEqual(masked["apiKey"], "[REDACTED]")
        self.assertEqual(masked["nested"]["authorization"], "[REDACTED]")
        self.assertEqual(masked["nested"]["image"], "[image/png data omitted]")
        self.assertEqual(
            _mask_sensitive_data(data={"apiKey": "secret-value"}),
            {"apiKey": "[REDACTED]"},
        )

    def test_runtime_is_disabled_when_configuration_is_incomplete(self) -> None:
        settings = Settings(
            LANGFUSE_ENABLED=True,
            LANGFUSE_PUBLIC_KEY="",
            LANGFUSE_SECRET_KEY="",
        )
        runtime = LangfuseRuntime(settings)

        self.assertFalse(runtime.configured)
        self.assertFalse(runtime.enabled)
        self.assertIsNone(runtime.status()["authenticated"])


if __name__ == "__main__":
    unittest.main()
