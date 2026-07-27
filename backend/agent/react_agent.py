from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from backend.agent.callbacks import (
    AgentErrorPayload,
    AgentRunContext,
    CallbackManager,
    ModelCallPayload,
    ModelResultPayload,
    ToolCallPayload,
    ToolResultPayload,
    TraceCallback,
)
from backend.config import get_or_init_settings
from backend.config.config import Settings
from backend.mcp_tool import McpClientManager, McpToolDescriptor
from backend.schemas import ChatMessage, ChatMeta, ChatRole
from backend.tools import ToolDefinition
from backend.validation import validate_tool_arguments


class OpenAIAgent:
    def __init__(
        self,
        tools: list[ToolDefinition],
        mcp_client_manager: McpClientManager,
        callbacks: list[Any] | None = None,
        config: Settings | None = None,
    ):
        if not config:
            config = Settings()
        self.config = config 
        self.tools = tools
        self.mcp_client_manager = mcp_client_manager
        self.client = AsyncOpenAI(
            api_key=self.config.openai_api_key,
            base_url=self.config.openai_base_url,
        )
        self.callbacks = callbacks or []

    async def run(self, messages: list[ChatMessage]) -> dict[str, Any]:
        final_state = None
        async for event in self.run_stream(messages):
            if event["type"] == "final":
                final_state = event["payload"]
        if final_state is None:
            raise RuntimeError("Agent run finished without a final payload.")
        return final_state

    async def run_stream(
        self,
        messages: list[ChatMessage],
        model_config: dict[str, Any] | None = None,
        mcp_server_ids: set[str] | None = None,
        system_prompt: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        trace: list[str] = []
        thinking: list[dict[str, Any]] = []
        selected_model = (model_config or {}).get("model", self.config.openai_model)
        client = self.client if model_config is None else AsyncOpenAI(
            api_key=model_config.get("apiKey") or self.config.openai_api_key,
            base_url=model_config.get("baseUrl") or self.config.openai_base_url,
        )
        context = AgentRunContext()
        callbacks = CallbackManager([TraceCallback(trace), *self.callbacks])
        working_messages = list(messages)
        await callbacks.on_agent_start(context, working_messages)

        try:
            mcp_tools = await self.mcp_client_manager.list_tools()
            if mcp_server_ids is not None:
                mcp_tools = [tool for tool in mcp_tools if tool.server_id in mcp_server_ids]
            tool_specs = self._build_tool_specs(mcp_tools)
            yield {"type": "trace", "payload": {"entry": trace[-1]}}
            preparation = self._thinking_step(
                thinking,
                phase="analysis",
                title="理解任务",
                detail=f"已读取对话上下文，并准备 {len(tool_specs)} 个可用工具。",
                status="complete",
                iteration=0,
            )
            yield {"type": "thinking", "payload": preparation}
            yield {"type": "status", "payload": {"message": self.config.status_model_started}}

            # Build the conversation history for Chat Completions API
            api_messages = self._build_api_messages(
                working_messages,
                system_prompt=system_prompt,
                attachments=attachments,
            )

            for iteration in range(self.config.max_model_iterations + 1):
                model_step = self._thinking_step(
                    thinking,
                    phase="reasoning",
                    title="分析并决定下一步" if iteration == 0 else "结合工具结果继续分析",
                    detail="正在评估上下文、可用工具与回答路径。",
                    status="active",
                    iteration=iteration,
                )
                yield {"type": "thinking", "payload": model_step}
                await callbacks.before_model(
                    context,
                    ModelCallPayload(
                        iteration=iteration,
                        model=selected_model,
                        messages=[],
                        tools=tool_specs,
                    ),
                )
                started_at = time.perf_counter()

                kwargs: dict[str, Any] = {
                    "model": selected_model,
                    "messages": api_messages,
                    "stream": True,
                }
                if tool_specs:
                    kwargs["tools"] = tool_specs
                    kwargs["tool_choice"] = "auto"
                if reasoning_effort and reasoning_effort != "none":
                    kwargs["reasoning_effort"] = reasoning_effort

                # Stream the response
                stream = await client.chat.completions.create(**kwargs)

                # Accumulate streamed content and tool calls
                content_buffer = ""
                reasoning_buffer = ""
                tool_call_buffers: dict[int, dict[str, Any]] = {}
                response_id = ""
                assistant_draft_id = str(uuid.uuid4())
                message_started = False
                reasoning_status_sent = False

                async for chunk in stream:
                    if chunk.id:
                        response_id = chunk.id

                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue

                    # Handle reasoning_content (DeepSeek thinking models)
                    reasoning_content = getattr(delta, "reasoning_content", None)
                    if reasoning_content:
                        reasoning_buffer += reasoning_content
                        if not reasoning_status_sent:
                            reasoning_status_sent = True
                            yield {"type": "status", "payload": {"message": "正在思考..."}}

                    # Stream text content token by token
                    if delta.content:
                        if not message_started:
                            message_started = True
                            yield {"type": "message_start", "payload": {"messageId": assistant_draft_id}}
                        content_buffer += delta.content
                        yield {
                            "type": "delta",
                            "payload": {"messageId": assistant_draft_id, "content": delta.content},
                        }

                    # Accumulate tool calls
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_call_buffers:
                                tool_call_buffers[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            if tc_delta.id:
                                tool_call_buffers[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tool_call_buffers[idx]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tool_call_buffers[idx]["arguments"] += tc_delta.function.arguments

                elapsed_ms = (time.perf_counter() - started_at) * 1000

                # End message if text was streamed
                if message_started:
                    yield {"type": "message_end", "payload": {"messageId": assistant_draft_id}}

                # Build tool_calls list from buffers
                tool_calls = [
                    tool_call_buffers[idx]
                    for idx in sorted(tool_call_buffers.keys())
                ]
                model_step["status"] = "complete"
                model_step["detail"] = (
                    f"分析完成，决定调用 {len(tool_calls)} 个工具。"
                    if tool_calls
                    else "分析完成，已形成最终回答。"
                )
                if reasoning_buffer:
                    model_step["detail"] += " 模型已完成内部推理。"
                yield {"type": "thinking", "payload": model_step}

                await callbacks.after_model(
                    context,
                    ModelResultPayload(
                        iteration=iteration,
                        model=selected_model,
                        response_id=response_id,
                        output_text=content_buffer.strip(),
                        function_call_count=len(tool_calls),
                        elapsed_ms=elapsed_ms,
                    ),
                )

                # No tool calls — we're done
                if not tool_calls:
                    if content_buffer.strip():
                        assistant_message = self._message("assistant", content_buffer.strip())
                        working_messages.append(assistant_message)
                    completed_step = self._thinking_step(
                        thinking,
                        phase="synthesis",
                        title="整理最终回答",
                        detail="已综合上下文与工具结果，完成回答。",
                        status="complete",
                        iteration=iteration,
                    )
                    yield {"type": "thinking", "payload": completed_step}
                    result = self._final_state(working_messages, trace, thinking)
                    await callbacks.on_agent_end(context, result)
                    yield {"type": "trace", "payload": {"entry": trace[-1]}}
                    yield {"type": "final", "payload": result}
                    return

                # Has tool calls — process them
                # Append assistant message with tool_calls to api_messages
                assistant_api_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": content_buffer or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                }
                api_messages.append(assistant_api_msg)

                for tc in tool_calls:
                    tool_name = tc["name"]
                    raw_arguments = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    tool_step = self._thinking_step(
                        thinking,
                        phase="tool",
                        title=f"调用 {tool_name}",
                        detail=self._tool_decision_summary(tool_name, raw_arguments),
                        status="active",
                        iteration=iteration,
                    )
                    yield {"type": "thinking", "payload": tool_step}
                    yield {
                        "type": "tool_start",
                        "payload": {
                            "toolCallId": tc["id"],
                            "toolCallName": tool_name,
                            "arguments": tc["arguments"] or "{}",
                        },
                    }
                    yield {
                        "type": "status",
                        "payload": {"message": f"调用工具 {tool_name}"},
                    }
                    tool_result = await self._run_tool(
                        callbacks=callbacks,
                        context=context,
                        iteration=iteration,
                        tool_name=tool_name,
                        arguments=raw_arguments,
                    )
                    working_messages.append(self._message("tool", tool_result, tool_name=tool_name))
                    tool_step["status"] = "complete"
                    tool_step["detail"] = f"{tool_name} 已返回结果，准备继续分析。"
                    yield {"type": "thinking", "payload": tool_step}
                    yield {
                        "type": "tool_result",
                        "payload": {
                            "toolCallId": tc["id"],
                            "messageId": working_messages[-1].id,
                            "content": tool_result,
                        },
                    }
                    yield {"type": "trace", "payload": {"entry": trace[-1], "output": tool_result}}
                    # Append tool result to api_messages
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })

                yield {"type": "status", "payload": {"message": self.config.status_model_started}}

            # Reached iteration limit
            limit_message = self.config.tool_iteration_limit_message
            assistant_message = self._message("assistant", limit_message)
            working_messages.append(assistant_message)
            yield {"type": "message_start", "payload": {"messageId": assistant_message.id}}
            yield {"type": "delta", "payload": {"messageId": assistant_message.id, "content": limit_message}}
            yield {"type": "message_end", "payload": {"messageId": assistant_message.id}}
            limit_step = self._thinking_step(
                thinking,
                phase="complete",
                title="达到执行上限",
                detail="已停止继续调用工具并返回当前结果。",
                status="complete",
                iteration=self.config.max_model_iterations,
            )
            yield {"type": "thinking", "payload": limit_step}
            result = self._final_state(working_messages, trace, thinking)
            await callbacks.on_agent_end(context, result)
            yield {"type": "trace", "payload": {"entry": trace[-1]}}
            yield {"type": "final", "payload": result}
        except Exception as exc:
            await callbacks.on_error(context, AgentErrorPayload(error=exc, stage="agent_run"))
            yield {"type": "trace", "payload": {"entry": trace[-1]}}
            raise

    async def _run_tool(
        self,
        callbacks: CallbackManager,
        context: AgentRunContext,
        iteration: int,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        local_tool = next((tool for tool in self.tools if tool.name == tool_name), None)
        if local_tool is not None:
            before_payload = ToolCallPayload(
                iteration=iteration,
                name=tool_name,
                arguments=arguments,
                source="local",
            )
            await callbacks.before_tool(context, before_payload)
            validate_tool_arguments(local_tool.parameters, arguments)
            started_at = time.perf_counter()
            result = await local_tool.execute(arguments)
            await callbacks.after_tool(
                context,
                ToolResultPayload(
                    iteration=iteration,
                    name=tool_name,
                    arguments=arguments,
                    output=result,
                    source="local",
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                ),
            )
            return result

        target = self._parse_mcp_tool(tool_name)
        if target is None:
            raise RuntimeError(f"Unknown tool requested: {tool_name}")

        server_id, name = target
        await callbacks.before_tool(
            context,
            ToolCallPayload(
                iteration=iteration,
                name=name,
                arguments=arguments,
                source="mcp",
                server_id=server_id,
            ),
        )
        started_at = time.perf_counter()
        result = await self.mcp_client_manager.call_tool(server_id, name, arguments)
        await callbacks.after_tool(
            context,
            ToolResultPayload(
                iteration=iteration,
                name=name,
                arguments=arguments,
                output=result,
                source="mcp",
                server_id=server_id,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
            ),
        )
        return result

    def _build_tool_specs(self, mcp_tools: list[McpToolDescriptor]) -> list[dict[str, Any]]:
        local_specs = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self.tools
        ]
        mcp_specs = [
            {
                "type": "function",
                "function": {
                    "name": f"mcp__{tool.server_id}__{tool.name}",
                    "description": tool.description or f"MCP tool {tool.name} from {tool.server_id}",
                    "parameters": tool.input_schema or {"type": "object", "properties": {}},
                },
            }
            for tool in mcp_tools
        ]
        return [*local_specs, *mcp_specs]

    def _build_api_messages(
        self,
        messages: list[ChatMessage],
        system_prompt: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert internal ChatMessage list to OpenAI Chat Completions API message format."""
        api_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt or self.config.system_prompt},
        ]
        for index, msg in enumerate(messages):
            content: Any = msg.content
            if attachments and index == len(messages) - 1 and msg.role == "user":
                content = [{"type": "text", "text": msg.content}]
                content.extend({
                    "type": "image_url",
                    "image_url": {"url": attachment["dataUrl"]},
                } for attachment in attachments if attachment.get("dataUrl"))
            api_messages.append({"role": msg.role, "content": content})
        return api_messages

    def _parse_mcp_tool(self, tool_name: str) -> tuple[str, str] | None:
        if not tool_name.startswith("mcp__"):
            return None
        _, server_id, *name_parts = tool_name.split("__")
        return server_id, "__".join(name_parts)

    def _message(self, role: ChatRole, content: str, tool_name: str | None = None) -> ChatMessage:
        return ChatMessage(
            id=str(uuid.uuid4()),
            role=role,
            content=content,
            createdAt=datetime.now(timezone.utc),
            meta=ChatMeta(toolName=tool_name) if tool_name else None,
        )

    def _extract_tasks(self, messages: list[ChatMessage]) -> list[str]:
        tasks: list[str] = []
        for message in reversed(messages):
            if message.role != "user":
                continue
            lines = [line.strip("- ").strip() for line in message.content.splitlines()]
            tasks = [line for line in lines if line][:4]
            if tasks:
                break
        return tasks

    def _final_state(
        self,
        messages: list[ChatMessage],
        trace: list[str],
        thinking: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "messages": messages,
            "trace": trace,
            "tasks": self._extract_tasks(messages),
            "thinking": thinking,
        }

    def _thinking_step(
        self,
        thinking: list[dict[str, Any]],
        *,
        phase: str,
        title: str,
        detail: str,
        status: str,
        iteration: int,
    ) -> dict[str, Any]:
        step = {
            "id": str(uuid.uuid4()),
            "phase": phase,
            "title": title,
            "detail": detail,
            "status": status,
            "iteration": iteration,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        thinking.append(step)
        return step

    @staticmethod
    def _tool_decision_summary(tool_name: str, arguments: dict[str, Any]) -> str:
        argument_count = len(arguments)
        return f"为推进任务，使用 {tool_name}，传入 {argument_count} 个参数。"

    def _model_result_payload(self, iteration: int, response: Any, started_at: float) -> ModelResultPayload:
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        return ModelResultPayload(
            iteration=iteration,
            model=self.config.openai_model,
            response_id=response.id,
            output_text=(choice.message.content or "").strip(),
            function_call_count=len(tool_calls),
            elapsed_ms=(time.perf_counter() - started_at) * 1000,
        )
