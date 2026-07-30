"""Streaming tool-using agent loop backed by an OpenAI-compatible chat API."""

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
    ContextPlanPayload,
    ContextPrunePayload,
    ModelCallPayload,
    ModelResultPayload,
    ToolCallPayload,
    ToolResultPayload,
    TraceCallback,
)
from backend.agent.contracts import AgentRunRequest
from backend.context import (
    build_context_plan,
    compose_api_messages,
    estimate_text_tokens,
    prune_old_tool_outputs,
)
from backend.config.config import Settings
from backend.mcp_tool import McpClientManager, McpToolDescriptor
from backend.permissions import check_permission
from backend.api.schemas import ChatMessage, ChatMeta, ChatRole
from backend.tools import ToolDefinition, validate_tool_arguments


class OpenAIAgent:
    """Execute a bounded model/tool loop and emit transport-neutral run events."""

    def __init__(
        self,
        tools: list[ToolDefinition],
        mcp_client_manager: McpClientManager,
        callbacks: list[Any] | None = None,
        config: Settings | None = None,
        skills: list[dict[str, Any]] | None = None,
    ):
        """初始化对象依赖和内部状态。"""
        if not config:
            config = Settings()
        self.config = config
        self.tools = tools
        self.mcp_client_manager = mcp_client_manager
        self.callbacks = callbacks or []
        # Only aliases selected for this request may resolve to the canonical
        # Skill tool. Arbitrary unknown function names still fail closed.
        self.skills = list(skills or [])

    async def run(
        self,
        request: AgentRunRequest,
    ) -> dict[str, Any]:
        """Consume a streaming run and return only its final state payload."""

        final_state = None
        async for event in self.run_stream(request):
            if event["type"] == "final":
                final_state = event["payload"]
        if final_state is None:
            raise RuntimeError("Agent run finished without a final payload.")
        return final_state

    async def run_stream(
        self,
        request: AgentRunRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield observable thinking, message, tool, trace, and final events."""

        trace: list[str] = []
        thinking: list[dict[str, Any]] = []
        selected_model = request.model_config.get("model", self.config.openai_model)
        client = AsyncOpenAI(
            api_key=request.model_config.get("apiKey") or self.config.openai_api_key,
            base_url=request.model_config.get("baseUrl") or self.config.openai_base_url,
        )
        context = AgentRunContext()
        callbacks = CallbackManager([TraceCallback(trace), *self.callbacks])

        try:
            mcp_tools = await self.mcp_client_manager.list_tools()
            if request.mcp_server_ids:
                mcp_tools = [
                    tool for tool in mcp_tools
                    if tool.server_id in request.mcp_server_ids
                ]
            tool_specs = self._build_tool_specs(mcp_tools)
            tool_definition_tokens = estimate_text_tokens(
                json.dumps(tool_specs, ensure_ascii=False, separators=(",", ":"))
            )
            context_plan = build_context_plan(
                [message for message in request.messages if message.content.strip()],
                system_prompt=request.system_prompt,
                user_context=request.user_context,
                model_config=request.model_config,
                tool_definition_tokens=tool_definition_tokens,
            )
            user_context = dict(request.user_context)
            if context_plan.summary:
                user_context["conversationSummary"] = (
                    "The following is a compacted summary of earlier conversation turns. "
                    "Treat it as continuity context, not a new user request.\n\n"
                    + context_plan.summary
                )
            working_messages = list(context_plan.messages)
            api_messages = compose_api_messages(
                context_plan.messages,
                system_prompt=request.system_prompt,
                user_context=user_context,
                attachments=request.attachments,
            )
            await callbacks.on_agent_start(context, working_messages)
            await callbacks.on_context_built(
                context,
                ContextPlanPayload(
                    input_message_count=len(request.messages),
                    active_message_count=len(context_plan.messages),
                    provider_message_count=len(api_messages),
                    compacted_message_count=len(
                        context_plan.compacted_message_ids
                    ),
                    summary_chars=len(context_plan.summary),
                    attachment_count=len(request.attachments),
                    auto_compacted=context_plan.auto_compacted,
                    budget=context_plan.budget.as_dict(),
                    breakdown=dict(context_plan.breakdown),
                ),
            )
            yield {
                "type": "context_state",
                "payload": {
                    **context_plan.as_dict(),
                    "loadedMemoryPaths": request.loaded_memory_paths,
                },
            }
            yield {"type": "trace", "payload": {"entry": trace[-1]}}
            if request.loaded_memory_paths:
                trace.append(
                    f"memory:eager_loaded:{len(request.loaded_memory_paths)} files"
                )
                yield {"type": "trace", "payload": {"entry": trace[-1]}}
            if mcp_tools:
                trace.append(f"prompt:mcp_dynamic_section:{len(mcp_tools)} tools")
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

            for iteration in range(self.config.max_model_iterations + 1):
                before_tool_chars = self._tool_output_chars(api_messages)
                before_tool_outputs = self._tool_output_contents(api_messages)
                api_messages = prune_old_tool_outputs(api_messages)
                after_tool_chars = self._tool_output_chars(api_messages)
                after_tool_outputs = self._tool_output_contents(api_messages)
                pruned_output_count = sum(
                    before != after
                    for before, after in zip(
                        before_tool_outputs,
                        after_tool_outputs,
                    )
                )
                if pruned_output_count:
                    await callbacks.on_context_pruned(
                        context,
                        ContextPrunePayload(
                            iteration=iteration,
                            pruned_output_count=pruned_output_count,
                            before_chars=before_tool_chars,
                            after_chars=after_tool_chars,
                        ),
                    )
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
                        messages=[dict(message) for message in api_messages],
                        tools=tool_specs,
                    ),
                )
                started_at = time.perf_counter()

                kwargs: dict[str, Any] = {
                    "model": selected_model,
                    "messages": api_messages,
                    "stream": True,
                    "max_tokens": int(request.model_config.get("maxOutputTokens") or 8192),
                }
                if tool_specs:
                    kwargs["tools"] = tool_specs
                    kwargs["tool_choice"] = "auto"
                if request.reasoning_effort and request.reasoning_effort != "none":
                    kwargs["reasoning_effort"] = request.reasoning_effort

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
                        # DeepSeek 会通过 reasoning_content 流式返回思考文本，这里同步到前端“思考过程”面板。
                        model_step["detail"] = reasoning_buffer
                        yield {"type": "thinking", "payload": model_step}
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
                if reasoning_buffer:
                    model_step["detail"] = reasoning_buffer
                else:
                    model_step["detail"] = (
                        f"分析完成，决定调用 {len(tool_calls)} 个工具。"
                        if tool_calls
                        else "分析完成，已形成最终回答。"
                    )
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
                        tool_calls=tool_calls,
                    ),
                )

                # No tool calls — we're done
                if not tool_calls:
                    if content_buffer.strip():
                        assistant_message = self._message("assistant", content_buffer.strip())
                        working_messages.append(assistant_message)
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
                    argument_error: Exception | None = None
                    try:
                        raw_arguments = self._decode_tool_arguments(tc["arguments"])
                    except Exception as exc:
                        # Malformed model-generated arguments are a recoverable
                        # tool observation; the model needs the reason in its
                        # tool result so it can repair the next call.
                        raw_arguments = {}
                        argument_error = exc
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
                    if argument_error is not None:
                        tool_result = await self._recoverable_tool_error(
                            callbacks=callbacks,
                            context=context,
                            tool_name=tool_name,
                            arguments=raw_arguments,
                            error=argument_error,
                        )
                    else:
                        tool_result = await self._run_tool(
                            callbacks=callbacks,
                            context=context,
                            iteration=iteration,
                            tool_name=tool_name,
                            arguments=raw_arguments,
                        )
                    working_messages.append(self._message("tool", tool_result, tool_name=tool_name))
                    tool_failed = self._tool_result_failed(tool_result)
                    tool_step["status"] = "error" if tool_failed else "complete"
                    tool_step["detail"] = (
                        f"{tool_name} 执行失败，已把原因返回模型以便修正。"
                        if tool_failed
                        else f"{tool_name} 已返回结果，准备继续分析。"
                    )
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
        """Execute one tool and convert its exceptions into model-visible results."""

        try:
            return await self._execute_tool(
                callbacks=callbacks,
                context=context,
                iteration=iteration,
                tool_name=tool_name,
                arguments=arguments,
            )
        except Exception as exc:
            # Tool failures must not abort the whole run. Cancellation and
            # process-level BaseException subclasses still propagate normally.
            return await self._recoverable_tool_error(
                callbacks=callbacks,
                context=context,
                tool_name=tool_name,
                arguments=arguments,
                error=exc,
            )

    async def _execute_tool(
        self,
        callbacks: CallbackManager,
        context: AgentRunContext,
        iteration: int,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Execute a validated local or MCP tool; callers own recovery."""

        local_tool = next((tool for tool in self.tools if tool.name == tool_name), None)
        selected_skill = self._selected_skill(tool_name)
        if local_tool is None and selected_skill is not None:
            local_tool = next(
                (tool for tool in self.tools if tool.name == "Skill"),
                None,
            )
            if local_tool is not None:
                arguments = self._skill_alias_arguments(tool_name, arguments)
        if local_tool is not None:
            permission_tool_name = local_tool.name
            decision = check_permission(
                permission_tool_name,
                self._permission_subject(permission_tool_name, arguments),
            )
            if decision.behavior != "allow":
                raise RuntimeError(
                    decision.reason
                    or f"Permission required for {permission_tool_name}"
                )
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
        decision = check_permission("mcp", f"{server_id}:{name}")
        if decision.behavior != "allow":
            raise RuntimeError(decision.reason or f"Permission required for MCP tool {server_id}:{name}")
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

    async def _recoverable_tool_error(
        self,
        *,
        callbacks: CallbackManager,
        context: AgentRunContext,
        tool_name: str,
        arguments: dict[str, Any],
        error: Exception,
    ) -> str:
        """Record a tool failure without allowing observability to break recovery."""

        try:
            await callbacks.on_error(
                context,
                AgentErrorPayload(
                    error=error,
                    stage="tool_run",
                    detail={"toolName": tool_name, "arguments": arguments},
                ),
            )
        except Exception:
            # Error reporting is fail-open: the model must still receive the
            # original tool failure even if an observability callback fails.
            pass
        message = str(error).strip() or "Tool execution failed without an error message."
        return json.dumps(
            {
                "ok": False,
                "tool": tool_name,
                "error": message,
                "errorType": type(error).__name__,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _decode_tool_arguments(raw_arguments: str) -> dict[str, Any]:
        """Decode provider arguments while enforcing the object tool contract."""

        if not raw_arguments:
            return {}
        decoded = json.loads(raw_arguments)
        if not isinstance(decoded, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        return decoded

    @staticmethod
    def _tool_result_failed(result: str) -> bool:
        """Recognize the common structured failure contracts used by tools."""

        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and (
                payload.get("ok") is False
                or payload.get("success") is False
                or payload.get("isError") is True
            )
        )

    def _selected_skill(self, tool_name: str) -> dict[str, Any] | None:
        """Resolve a direct function name only against this request's Skills."""

        return next(
            (
                skill
                for skill in self.skills
                if skill.get("enabled", True)
                and tool_name
                in {
                    str(skill.get("id") or ""),
                    str(skill.get("name") or ""),
                }
            ),
            None,
        )

    @staticmethod
    def _skill_alias_arguments(
        skill_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a provider's direct Skill call to the canonical Skill schema."""

        raw_args = arguments.get("args")
        supplied_skill = arguments.get("skill")
        if raw_args is None and supplied_skill not in (None, "", skill_name):
            # Some compatible providers change the function name to the Skill
            # name, then put the user's topic into the old `skill` field.
            raw_args = supplied_skill
        if raw_args is None:
            raw_args = next(
                (
                    value
                    for key, value in arguments.items()
                    if key != "skill" and value not in (None, "")
                ),
                "",
            )
        if not isinstance(raw_args, str):
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        return {"skill": skill_name, "args": raw_args}

    @staticmethod
    def _permission_subject(tool_name: str, arguments: dict[str, Any]) -> str:
        """为不同工具提取可匹配的权限对象，例如 Bash 命令或文件路径。"""
        if tool_name == "Bash":
            return str(arguments.get("command") or tool_name)
        if tool_name in {"Read", "Write", "Edit", "Glob", "Grep", "LS", "NotebookEdit"}:
            return str(arguments.get("file_path") or arguments.get("path") or tool_name)
        if tool_name == "WebFetch":
            return str(arguments.get("url") or tool_name)
        if tool_name == "WebSearch":
            return str(arguments.get("query") or tool_name)
        if tool_name == "ReadMcpResourceTool":
            return f"{arguments.get('server_id') or arguments.get('serverId') or ''}:{arguments.get('uri') or ''}"
        if tool_name == "Skill":
            return str(arguments.get("skill") or tool_name)
        return tool_name

    def _build_tool_specs(self, mcp_tools: list[McpToolDescriptor]) -> list[dict[str, Any]]:
        """把本地工具和 MCP 工具转成模型 tool schema。"""
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

    def _parse_mcp_tool(self, tool_name: str) -> tuple[str, str] | None:
        """从工具名解析 MCP server ID 和真实工具名。"""
        if not tool_name.startswith("mcp__"):
            return None
        _, server_id, *name_parts = tool_name.split("__")
        return server_id, "__".join(name_parts)

    def _message(self, role: ChatRole, content: str, tool_name: str | None = None) -> ChatMessage:
        """创建一条带时间戳的 ChatMessage。"""
        return ChatMessage(
            id=str(uuid.uuid4()),
            role=role,
            content=content,
            createdAt=datetime.now(timezone.utc),
            meta=ChatMeta(toolName=tool_name) if tool_name else None,
        )

    def _final_state(
        self,
        messages: list[ChatMessage],
        trace: list[str],
        thinking: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """组装 Agent run 的最终内部状态。"""
        return {
            "messages": messages,
            "trace": trace,
            "tasks": [],
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
        """生成一条可映射为 reasoning 的思考摘要。"""
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
        """把工具调用计划渲染成 thinking 摘要。"""
        argument_count = len(arguments)
        return f"为推进任务，使用 {tool_name}，传入 {argument_count} 个参数。"

    @staticmethod
    def _tool_output_contents(messages: list[dict[str, Any]]) -> list[str]:
        """Return tool output values only for before/after pruning comparison."""

        return [
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "tool"
            and isinstance(message.get("content"), str)
        ]

    @classmethod
    def _tool_output_chars(cls, messages: list[dict[str, Any]]) -> int:
        """Count tool-output characters without exposing their content to logs."""

        return sum(len(content) for content in cls._tool_output_contents(messages))

    def _model_result_payload(self, iteration: int, response: Any, started_at: float) -> ModelResultPayload:
        """把模型响应转换为生命周期回调 payload。"""
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
