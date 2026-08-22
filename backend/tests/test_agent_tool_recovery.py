"""Regression tests for recoverable tool failures inside the model loop."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agent.contracts import AgentRunRequest
from backend.agent.react_agent import OpenAIAgent
from backend.agui import translate_agent_events
from backend.api.schemas import ChatMessage
from backend.config.config import Settings
from backend.mcp_tool import McpClientManager
from backend.tools import ToolDefinition


class _ChunkStream:
    """Provide the minimal async stream contract used by the OpenAI client."""

    def __init__(self, chunks: list[SimpleNamespace]):
        self._chunks = chunks

    def __aiter__(self):
        async def iterate():
            for chunk in self._chunks:
                yield chunk

        return iterate()


def _chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_call: SimpleNamespace | None = None,
):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=[tool_call] if tool_call is not None else None,
    )
    return SimpleNamespace(
        id="response-id",
        choices=[SimpleNamespace(delta=delta)],
    )


class AgentToolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_ends_before_text_stream_starts(self) -> None:
        stream = _ChunkStream(
            [
                _chunk(reasoning_content="reason-1"),
                _chunk(reasoning_content="reason-2"),
                _chunk(content="answer"),
                _chunk(content="!"),
            ]
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=stream)),
            )
        )
        agent = OpenAIAgent()
        request = AgentRunRequest(
            messages=[
                ChatMessage(
                    id="user-stream-order",
                    role="user",
                    content="answer with reasoning",
                    createdAt=datetime.now(timezone.utc),
                )
            ],
            system_prompt="Finish reasoning before visible text.",
            user_context={},
            model_config={
                "model": "test-model",
                "apiKey": "test",
                "baseUrl": "http://example.test/v1",
            },
        )

        with patch("backend.agent.react_agent.AsyncOpenAI", return_value=fake_client):
            runtime = await agent.create_runtime(
                request,
                [],
                McpClientManager([]),
                config=Settings(OPENAI_API_KEY="test"),
            )
            events = [event async for event in agent.run_stream_react(runtime)]

        stream_events = [
            event
            for event in events
            if event["type"]
            in {
                "reasoning_start",
                "reasoning_delta",
                "reasoning_end",
                "message_start",
                "delta",
                "message_end",
            }
        ]
        self.assertEqual(
            [event["type"] for event in stream_events],
            [
                "reasoning_start",
                "reasoning_delta",
                "reasoning_delta",
                "reasoning_end",
                "message_start",
                "delta",
                "delta",
                "message_end",
            ],
        )
        self.assertEqual(
            [
                event["payload"]["content"]
                for event in stream_events
                if event["type"] == "reasoning_delta"
            ],
            ["reason-1", "reason-2"],
        )
        self.assertEqual(
            len(
                {
                    event["payload"]["reasoningId"]
                    for event in stream_events
                    if event["type"].startswith("reasoning_")
                }
            ),
            1,
        )

        async def replay_events():
            for event in events:
                yield event

        translated = [
            event
            async for event in translate_agent_events(
                replay_events(), "thread-stream-order", "run-stream-order"
            )
        ]
        event_types = [event.type for event in translated]
        text_start = event_types.index("TEXT_MESSAGE_START")
        self.assertEqual(
            event_types[text_start - 2 : text_start + 1],
            ["REASONING_MESSAGE_END", "REASONING_END", "TEXT_MESSAGE_START"],
        )
        reasoning_start = max(
            index
            for index, event_type in enumerate(event_types[:text_start])
            if event_type == "REASONING_START"
        )
        self.assertEqual(
            len(
                {
                    event.message_id
                    for event in translated[reasoning_start:text_start]
                    if event.type.startswith("REASONING_")
                }
            ),
            1,
        )
        self.assertLess(
            event_types.index("TEXT_MESSAGE_CONTENT", text_start),
            event_types.index("TEXT_MESSAGE_END", text_start),
        )

    async def test_tool_exception_is_returned_to_model_and_next_iteration_runs(self) -> None:
        async def fail_read(_: dict) -> str:
            raise ValueError("path is outside workspace: /tmp/result.md")

        tool = ToolDefinition(
            name="Read",
            description="Read a file.",
            parameters={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
                "additionalProperties": False,
            },
            execute=fail_read,
        )
        first_stream = _ChunkStream(
            [
                _chunk(
                    tool_call=SimpleNamespace(
                        index=0,
                        id="call-read",
                        function=SimpleNamespace(
                            name="Read",
                            arguments='{"file_path":"/tmp/result.md"}',
                        ),
                    )
                )
            ]
        )
        second_stream = _ChunkStream([_chunk(content="已根据错误原因改用其他方案。")])
        create = AsyncMock(side_effect=[first_stream, second_stream])
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        )
        agent = OpenAIAgent()
        request = AgentRunRequest(
            messages=[
                ChatMessage(
                    id="user-1",
                    role="user",
                    content="read the result",
                    createdAt=datetime.now(timezone.utc),
                )
            ],
            system_prompt="Continue after recoverable tool errors.",
            user_context={},
            model_config={
                "model": "test-model",
                "apiKey": "test",
                "baseUrl": "http://example.test/v1",
            },
        )

        with patch("backend.agent.react_agent.AsyncOpenAI", return_value=fake_client):
            runtime = await agent.create_runtime(
                request,
                [tool],
                McpClientManager([]),
                config=Settings(
                    OPENAI_API_KEY="test",
                    MAX_MODEL_ITERATIONS=2,
                ),
            )
            events = [event async for event in agent.run_stream_react(runtime)]

        tool_result_event = next(
            event for event in events if event["type"] == "tool_result"
        )
        failure = json.loads(tool_result_event["payload"]["content"])
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["errorType"], "ValueError")
        self.assertIn("outside workspace", failure["error"])
        self.assertEqual(create.await_count, 2)

        second_model_messages = create.await_args_list[1].kwargs["messages"]
        self.assertEqual(second_model_messages[-1]["role"], "tool")
        self.assertEqual(
            json.loads(second_model_messages[-1]["content"])["error"],
            "path is outside workspace: /tmp/result.md",
        )
        final = next(event for event in events if event["type"] == "final")
        self.assertEqual(final["payload"]["output"], "已根据错误原因改用其他方案。")
        self.assertNotIn("messages", final["payload"])
        self.assertFalse(any(event["type"] == "RUN_ERROR" for event in events))


if __name__ == "__main__":
    unittest.main()
