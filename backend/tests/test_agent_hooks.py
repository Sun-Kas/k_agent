"""Contracts for declarative observers and compiled Agent middleware."""

from __future__ import annotations

import asyncio
import unittest

from backend.agent.hooks import (
    AgentEventType,
    AgentPipelineDefinition,
    AgentRunContext,
    ModelCallCompleted,
    ModelCallPayload,
    ModelReasoningDelta,
    ModelResultPayload,
    ModelTextDelta,
    ToolCallRequest,
    ToolCompletedEvent,
    ToolStartedEvent,
    after_model,
    before_model,
    observe,
    wrap_model_call,
    wrap_tool_call,
)
from backend.agent.hooks.builtins import build_k_agent_pipeline_definition
from backend.agent.hooks.observers import ObserverDispatcher


class ObserverDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_failures_are_isolated_and_order_is_stable(self) -> None:
        calls: list[str] = []

        @observe(AgentEventType.TOOL_STARTED, order=20)
        async def second(event) -> None:
            calls.append(f"second:{event.payload.call_id}")

        @observe(AgentEventType.TOOL_STARTED, order=10)
        async def broken(_event) -> None:
            calls.append("broken")
            raise RuntimeError("observer details must not escape")

        dispatcher = ObserverDispatcher([second, broken])
        definition = AgentPipelineDefinition.compile()
        runtime = definition.bind_runtime(
            context=AgentRunContext(run_id="run-1"),
            observers=[second, broken],
        )

        async def preflight(_request) -> None:
            return None

        async def execute(_request) -> str:
            return "ok"

        await runtime.run_tool(
            ToolCallRequest(
                call_id="call-1",
                iteration=0,
                requested_name="Read",
                canonical_name="Read",
                arguments={},
                source="local",
            ),
            preflight=preflight,
            execute=execute,
        )

        self.assertEqual(calls, ["broken", "second:call-1"])
        self.assertIsInstance(dispatcher, ObserverDispatcher)

    async def test_parallel_same_name_tools_have_distinct_operation_ids(self) -> None:
        events = []

        class Collector:
            async def handle(self, event) -> None:
                if isinstance(event, (ToolStartedEvent, ToolCompletedEvent)):
                    events.append(event)

        runtime = AgentPipelineDefinition.compile().bind_runtime(
            context=AgentRunContext(run_id="run-parallel"),
            observers=[Collector()],
        )

        async def preflight(_request) -> None:
            return None

        async def execute(request) -> str:
            await asyncio.sleep(0)
            return request.call_id

        def request(call_id: str) -> ToolCallRequest:
            return ToolCallRequest(
                call_id=call_id,
                iteration=0,
                requested_name="Read",
                canonical_name="Read",
                arguments={},
                source="local",
            )

        await asyncio.gather(
            runtime.run_tool(request("call-a"), preflight=preflight, execute=execute),
            runtime.run_tool(request("call-b"), preflight=preflight, execute=execute),
        )

        started = [event for event in events if isinstance(event, ToolStartedEvent)]
        completed = [event for event in events if isinstance(event, ToolCompletedEvent)]
        self.assertEqual({event.payload.call_id for event in started}, {"call-a", "call-b"})
        self.assertEqual(
            {event.payload.operation_id for event in started},
            {event.payload.operation_id for event in completed},
        )
        self.assertEqual(len({event.payload.operation_id for event in started}), 2)


class MiddlewarePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_read_middleware_strips_legacy_escalation_fields(self) -> None:
        """The built-in registration must rewrite before the sealed preflight."""

        received: list[dict] = []
        runtime = build_k_agent_pipeline_definition().bind_runtime(
            context=AgentRunContext(run_id="run-default-middleware")
        )

        async def preflight(request) -> None:
            received.append(dict(request.arguments))

        async def execute(request) -> str:
            received.append(dict(request.arguments))
            return "ok"

        result = await runtime.run_tool(
            ToolCallRequest(
                call_id="call-read-legacy",
                iteration=0,
                requested_name="Read",
                canonical_name="Read",
                arguments={
                    "file_path": "/workspace/result.md",
                    "sandbox_permissions": "require_escalated",
                    "escalation_scope": "workspace",
                    "escalation_resource": "/workspace/result.md",
                },
                source="local",
            ),
            preflight=preflight,
            execute=execute,
        )

        expected = {"file_path": "/workspace/result.md"}
        self.assertEqual(result.output, "ok")
        self.assertEqual(received, [expected, expected])

    async def test_default_read_middleware_preserves_bash_escalation_fields(self) -> None:
        """Write-capable tools still need escalation metadata for permission checks."""

        received: list[dict] = []
        runtime = build_k_agent_pipeline_definition().bind_runtime(
            context=AgentRunContext(run_id="run-default-middleware-bash")
        )

        async def preflight(request) -> None:
            received.append(dict(request.arguments))

        async def execute(_request) -> str:
            return "ok"

        arguments = {
            "command": "pwd",
            "sandbox_permissions": "require_escalated",
            "escalation_scope": "workspace",
        }
        await runtime.run_tool(
            ToolCallRequest(
                call_id="call-bash-escalated",
                iteration=0,
                requested_name="Bash",
                canonical_name="Bash",
                arguments=arguments,
                source="local",
            ),
            preflight=preflight,
            execute=execute,
        )

        self.assertEqual(received, [arguments])

    async def test_node_hooks_and_model_wrappers_follow_stack_order(self) -> None:
        calls: list[str] = []

        @before_model(order=10)
        async def before_first(_state, _runtime):
            calls.append("before:first")

        @before_model(order=20)
        async def before_second(_state, _runtime):
            calls.append("before:second")

        @after_model(order=10)
        async def after_first(_state, _runtime):
            calls.append("after:first")

        @after_model(order=20)
        async def after_second(_state, _runtime):
            calls.append("after:second")

        @wrap_model_call(order=10)
        async def outer(request, call_next):
            calls.append("wrap:outer:enter")
            async for event in call_next(request):
                yield event
            calls.append("wrap:outer:exit")

        @wrap_model_call(order=20)
        async def inner(request, call_next):
            calls.append("wrap:inner:enter")
            async for event in call_next(request):
                yield event
            calls.append("wrap:inner:exit")

        runtime = AgentPipelineDefinition.compile(
            [before_second, outer, after_first, inner, before_first, after_second]
        ).bind_runtime(context=AgentRunContext(run_id="run-model"))

        async def terminal(request):
            calls.append("terminal")
            yield ModelTextDelta("ok")
            yield ModelCallCompleted(
                ModelResultPayload(
                    iteration=request.iteration,
                    model=request.model,
                    response_id="response",
                    output_text="ok",
                    function_call_count=0,
                    elapsed_ms=1,
                    operation_id=request.operation_id,
                )
            )

        request = ModelCallPayload(
            iteration=0,
            model="test",
            messages=(),
            tools=(),
        )
        result = [event async for event in runtime.stream_model(request, terminal)]

        self.assertEqual([type(event) for event in result], [ModelTextDelta, ModelCallCompleted])
        self.assertEqual(
            calls,
            [
                "before:first",
                "before:second",
                "wrap:outer:enter",
                "wrap:inner:enter",
                "terminal",
                "wrap:inner:exit",
                "wrap:outer:exit",
                "after:second",
                "after:first",
            ],
        )

    async def test_model_retry_is_rejected_after_visible_delta(self) -> None:
        @wrap_model_call()
        async def invalid_retry(request, call_next):
            async for event in call_next(request):
                yield event
            async for event in call_next(request):
                yield event

        runtime = AgentPipelineDefinition.compile([invalid_retry]).bind_runtime(
            context=AgentRunContext(run_id="run-retry")
        )

        async def terminal(request):
            yield ModelReasoningDelta("visible")
            yield ModelCallCompleted(
                ModelResultPayload(
                    iteration=0,
                    model=request.model,
                    response_id="response",
                    output_text="",
                    function_call_count=0,
                    elapsed_ms=1,
                    operation_id=request.operation_id,
                )
            )

        with self.assertRaisesRegex(RuntimeError, "visible stream delta"):
            _ = [
                event
                async for event in runtime.stream_model(
                    ModelCallPayload(0, "test", (), ()), terminal
                )
            ]

    async def test_tool_wrapper_override_reenters_sealed_preflight(self) -> None:
        calls: list[str] = []

        @wrap_tool_call()
        async def rewrite(request, call_next):
            return await call_next(request.override(arguments={"path": "/safe"}))

        runtime = AgentPipelineDefinition.compile([rewrite]).bind_runtime(
            context=AgentRunContext(run_id="run-tool")
        )

        async def preflight(request) -> None:
            calls.append(f"preflight:{request.arguments['path']}")

        async def execute(request) -> str:
            calls.append(f"execute:{request.arguments['path']}")
            return "ok"

        result = await runtime.run_tool(
            ToolCallRequest(
                call_id="call-tool",
                iteration=0,
                requested_name="Read",
                canonical_name="Read",
                arguments={"path": "/unsafe"},
                source="local",
            ),
            preflight=preflight,
            execute=execute,
        )

        self.assertEqual(result.output, "ok")
        self.assertEqual(calls, ["preflight:/safe", "execute:/safe"])


if __name__ == "__main__":
    unittest.main()
