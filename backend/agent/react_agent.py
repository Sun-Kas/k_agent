"""OpenAI 兼容聊天 API 上的流式 React 主循环（模型 ↔ 工具交替）。

pipeline 核心：`KAgentRunner` 复用一个 `OpenAIAgent`，每轮调用
`create_runtime` 拼出请求级运行信息，再由同一个 Agent 的 `run_stream`
执行 thinking/message/tool/trace/final 循环，最后交给 `agui` 转 AG-UI。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from backend.agent.hooks import (
    AgentPipelineDefinition,
    AgentPipelineRuntime,
    AgentRunContext,
    ContextPlanPayload,
    ContextPrunePayload,
    ModelCallCompleted,
    ModelCallPayload,
    ModelReasoningDelta,
    ModelResultPayload,
    ModelStreamEvent,
    ModelTextDelta,
    ToolCallRequest,
    ToolCallResult,
    TraceObserver,
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
from backend.permissions import PermissionDecision, check_permission, check_permissions
from backend.api.schemas import ChatMessage, ChatMeta, ChatRole
from backend.approvals import canonical_json_sha256
from backend.sandbox import is_domain_allowed, notice_from_tool_result
from backend.tools import ToolDefinition, validate_tool_arguments
from backend.tools.streaming import reset_tool_output_sink, set_tool_output_sink


_READ_ONLY_LOCAL_TOOLS = frozenset({"Read", "Glob", "Grep", "LS"})
_ESCALATABLE_LOCAL_TOOLS = frozenset({"Bash", "Write", "Edit", "NotebookEdit"})


class OpenAIAgent:
    """进程内无状态 Agent；请求状态只存在于 runtime 字典与函数局部变量。"""

    __slots__ = ()

    async def create_runtime(
        self,
        request: AgentRunRequest,
        tools: list[ToolDefinition],
        mcp_client_manager: McpClientManager,
        observers: list[Any] | None = None,
        pipeline_definition: AgentPipelineDefinition | None = None,
        run_context: AgentRunContext | None = None,
        config: Settings | None = None,
        skills: list[dict[str, Any]] | None = None,
        approval_handler: Callable[
            [str, PermissionDecision, dict[str, Any]],
            Awaitable[dict[str, Any]],
        ] | None = None,
    ) -> dict[str, Any]:
        """准备模型循环需要的请求级数据；不执行模型或工具循环。"""

        runtime_config = config or Settings()
        runtime_tools = list(tools)
        trace: list[str] = []
        thinking: list[dict[str, Any]] = []
        context = run_context or AgentRunContext()
        context.metadata["permission_mode"] = request.permission_mode
        pipeline = (pipeline_definition or AgentPipelineDefinition.compile()).bind_runtime(
            context=context,
            observers=[TraceObserver(trace), *list(observers or [])],
        )
        try:
            mcp_tools = await mcp_client_manager.list_tools()
            # manager 可能复用连接池，但本轮只能暴露 Access Layer 选中的 server。
            if request.mcp_server_ids:
                mcp_tools = [
                    tool
                    for tool in mcp_tools
                    if tool.server_id in request.mcp_server_ids
                ]
            tool_specs = self._build_tool_specs(mcp_tools, runtime_tools)
            tool_definition_tokens = estimate_text_tokens(
                json.dumps(tool_specs, ensure_ascii=False, separators=(",", ":"))
            )
            context_plan = build_context_plan(
                [message for message in request.messages if message.carries_context()],
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
            selected_model = request.model_config.get(
                "model", runtime_config.openai_model
            )
            client = AsyncOpenAI(
                api_key=(
                    request.model_config.get("apiKey")
                    or runtime_config.openai_api_key
                ),
                base_url=(
                    request.model_config.get("baseUrl")
                    or runtime_config.openai_base_url
                ),
            )
        except Exception as exc:
            # Runtime 准备失败发生在流开始前，仍应通知观测回调后再交给上层转 RUN_ERROR。
            await pipeline.emit_failure(exc, stage="runtime_create")
            raise

        return {
            "request": request,
            "config": runtime_config,
            "tools": runtime_tools,
            "mcp_client_manager": mcp_client_manager,
            "skills": list(skills or []),
            "approval_handler": approval_handler,
            "approved_targets": set(),
            "pipeline": pipeline,
            "context": context,
            "trace": trace,
            "thinking": thinking,
            "mcp_tools": mcp_tools,
            "tool_specs": tool_specs,
            "context_plan": context_plan,
            "working_messages": working_messages,
            "api_messages": api_messages,
            "selected_model": selected_model,
            "client": client,
        }

    async def run(
        self,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        """消费流式 run，只返回最终 `final` 载荷（非流式调用方用）。"""

        final_state = None
        async for event in self.run_stream_react(runtime):
            if event["type"] == "final":
                final_state = event["payload"]
        if final_state is None:
            raise RuntimeError("Agent run finished without a final payload.")
        return final_state

    async def run_stream_react(
        self,
        runtime: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """驱动 ReAct 循环：模型推理 → 工具执行 → 观察结果，直到完成或达到上限。"""

        # create_runtime 已完成所有循环前准备；这里仅加载并驱动执行。
        request: AgentRunRequest = runtime["request"]  # 本轮已拼好的 prompt/消息/权限入参
        config: Settings = runtime["config"]  # 进程配置（迭代上限、默认模型、状态文案等）
        trace: list[str] = runtime["trace"]  # 本轮内部轨迹字符串，最终打进 final.payload
        thinking: list[dict[str, Any]] = runtime["thinking"]  # UI 思考步骤累计，供 thinking 事件
        selected_model: str = runtime["selected_model"]  # 实际发给 provider 的模型 id
        client: AsyncOpenAI = runtime["client"]  # 本轮专用 OpenAI 兼容客户端（含 apiKey/baseUrl）
        context: AgentRunContext = runtime["context"]  # 回调共享的 run 元数据（含 permission_mode）
        pipeline: AgentPipelineRuntime = runtime["pipeline"]  # 本轮 Observer/Middleware 执行管线
        mcp_tools: list[McpToolDescriptor] = runtime["mcp_tools"]  # 本轮已过滤的 MCP 工具描述
        tool_specs: list[dict[str, Any]] = runtime["tool_specs"]  # 发给 chat.completions 的 tools schema
        context_plan = runtime["context_plan"]  # 裁剪/摘要后的上下文预算方案
        working_messages: list[ChatMessage] = runtime["working_messages"]  # 回写 Access Layer 的会话消息
        api_messages: list[dict[str, Any]] = runtime["api_messages"]  # 发给 provider 的原生消息（含 system）

        agent_scope = pipeline.agent_run(working_messages)
        try:
            await agent_scope.__aenter__()
            await pipeline.emit_context_built(
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
            yield {"type": "status", "payload": {"message": config.status_model_started}}

            start_iteration = 0
            resume_checkpoint = runtime.get("resume_checkpoint")
            if isinstance(resume_checkpoint, dict):
                if resume_checkpoint.get("kind") != "react_tool_boundary":
                    raise RuntimeError("K Agent checkpoint is not a ReAct tool boundary")
                checkpoint_messages = resume_checkpoint.get("apiMessages")
                pending_calls = resume_checkpoint.get("pendingCalls")
                pending_index = resume_checkpoint.get("pendingIndex")
                checkpoint_iteration = resume_checkpoint.get("iteration")
                if (
                    not isinstance(checkpoint_messages, list)
                    or not isinstance(pending_calls, list)
                    or not isinstance(pending_index, int)
                    or not isinstance(checkpoint_iteration, int)
                    or pending_index < 0
                    or pending_index >= len(pending_calls)
                ):
                    raise RuntimeError("K Agent ReAct checkpoint is incomplete")
                api_messages = [dict(message) for message in checkpoint_messages]
                resume_decision = runtime.get("resume_decision")
                if not isinstance(resume_decision, dict):
                    raise RuntimeError("K Agent resume decision is missing")
                decision_payload = resume_decision.get("payload")
                approved = (
                    resume_decision.get("status") == "resolved"
                    and isinstance(decision_payload, dict)
                    and decision_payload.get("approved") is True
                )

                for index in range(pending_index, len(pending_calls)):
                    tc = pending_calls[index]
                    if not isinstance(tc, dict):
                        raise RuntimeError("K Agent checkpoint contains an invalid tool call")
                    call_id = str(tc.get("id") or "")
                    tool_name = str(tc.get("name") or "")
                    if not call_id or not tool_name:
                        raise RuntimeError("K Agent checkpoint tool identity is missing")
                    raw_arguments = self._decode_tool_arguments(str(tc.get("arguments") or "{}"))
                    # 首个调用在旧 run 已显示过卡片，但 SessionStore 已在 terminal
                    # 边界清掉未完成 buffer。Resume 必须重发同 id START/ARGS/END，
                    # 前端按 id 原位更新，持久层则重新建立完整 tool-call 配对。
                    yield {
                        "type": "tool_start",
                        "payload": {
                            "toolCallId": call_id,
                            "toolCallName": tool_name,
                            "arguments": str(tc.get("arguments") or "{}"),
                        },
                    }
                    if index == pending_index and not approved:
                        tool_result = (
                            f"Tool {tool_name} was not executed because the user denied "
                            "or cancelled the approval request."
                        )
                    else:
                        if index == pending_index:
                            runtime["_resume_authorization"] = {
                                "callId": call_id,
                                "requestHash": runtime.get("resume_request_hash"),
                            }
                        runtime["_react_tool_boundary"] = {
                            "version": 1,
                            "kind": "react_tool_boundary",
                            "iteration": checkpoint_iteration,
                            "pendingIndex": index,
                            "pendingCalls": [dict(item) for item in pending_calls],
                            "apiMessages": [dict(item) for item in api_messages],
                        }
                        tool_result = await self._run_tool(
                            runtime=runtime,
                            iteration=checkpoint_iteration,
                            call_id=call_id,
                            tool_name=tool_name,
                            arguments=raw_arguments,
                        )
                    tool_message = self._message("tool", tool_result, tool_name=tool_name)
                    working_messages.append(tool_message)
                    yield {
                        "type": "tool_result",
                        "payload": {
                            "toolCallId": call_id,
                            "messageId": tool_message.id,
                            "content": tool_result,
                        },
                    }
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": tool_result,
                    })
                start_iteration = checkpoint_iteration + 1
                yield {"type": "status", "payload": {"message": config.status_model_started}}

            # 循环的正常出口是下面「模型不再请求工具」的分支；跑满迭代次数属于
            # 异常兜底，会在循环后返回一条上限提示，而不是让 run 无限继续。
            for iteration in range(start_iteration, config.max_model_iterations + 1):
                # 每轮开头裁剪一次：工具结果是上下文增长最快的来源，
                # 若等到超预算再处理，本轮请求已经发不出去了。
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
                    await pipeline.emit_context_pruned(
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
                model_request = ModelCallPayload(
                    iteration=iteration,
                    model=selected_model,
                    messages=tuple(dict(message) for message in api_messages),
                    tools=tuple(tool_specs),
                    reasoning_effort=request.reasoning_effort,
                )
                assistant_draft_id = str(uuid.uuid4())
                message_started = False
                reasoning_status_sent = False
                content_buffer = ""
                reasoning_buffer = ""
                model_result: ModelResultPayload | None = None

                async def provider_terminal(current: ModelCallPayload):
                    async for item in self._provider_model_stream(
                        current,
                        client=client,
                        config=config,
                        max_output_tokens=int(
                            request.model_config.get("maxOutputTokens") or 8192
                        ),
                    ):
                        yield item

                async for model_event in pipeline.stream_model(
                    model_request,
                    provider_terminal,
                ):
                    if isinstance(model_event, ModelReasoningDelta):
                        reasoning_buffer += model_event.content
                        # DeepSeek 会通过 reasoning_content 流式返回思考文本，这里同步到前端“思考过程”面板。
                        model_step["detail"] = reasoning_buffer
                        yield {"type": "thinking", "payload": model_step}
                        if not reasoning_status_sent:
                            reasoning_status_sent = True
                            yield {"type": "status", "payload": {"message": "正在思考..."}}

                    elif isinstance(model_event, ModelTextDelta):
                        if not message_started:
                            message_started = True
                            yield {"type": "message_start", "payload": {"messageId": assistant_draft_id}}
                        content_buffer += model_event.content
                        yield {
                            "type": "delta",
                            "payload": {
                                "messageId": assistant_draft_id,
                                "content": model_event.content,
                            },
                        }
                    elif isinstance(model_event, ModelCallCompleted):
                        model_result = model_event.result

                if model_result is None:
                    raise RuntimeError("Model pipeline finished without a result")
                tool_calls = [dict(item) for item in model_result.tool_calls]

                # End message if text was streamed
                if message_started:
                    yield {"type": "message_end", "payload": {"messageId": assistant_draft_id}}

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

                # No tool calls — we're done
                if not tool_calls:
                    if content_buffer.strip():
                        assistant_message = self._message("assistant", content_buffer.strip())
                        working_messages.append(assistant_message)
                    result = self._final_state(working_messages, trace, thinking)
                    agent_scope.complete(result)
                    await agent_scope.__aexit__(None, None, None)
                    yield {"type": "trace", "payload": {"entry": trace[-1]}}
                    yield {"type": "final", "payload": result}
                    return

                # Has tool calls — process them
                # Append assistant message with tool_calls to api_messages
                # 这条带 tool_calls 的 assistant 消息只进 api_messages，不进
                # working_messages：它是 provider 协议要求的配对前提（后面每条
                # tool 消息都要能通过 tool_call_id 找到它），但对会话历史无意义。
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

                for tool_call_index, tc in enumerate(tool_calls):
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
                        await pipeline.emit_failure(
                            argument_error,
                            stage="tool_arguments",
                            detail={"toolName": tool_name, "callId": tc["id"]},
                        )
                        tool_result = self._recoverable_tool_error(
                            tool_name=tool_name,
                            error=argument_error,
                        )
                    else:
                        # 若权限层在工具真正执行前产生 terminal Interrupt，恢复所需
                        # 的 provider 消息与剩余调用列表必须精确停在这个边界。
                        runtime["_react_tool_boundary"] = {
                            "version": 1,
                            "kind": "react_tool_boundary",
                            "iteration": iteration,
                            "pendingIndex": tool_call_index,
                            "pendingCalls": [dict(item) for item in tool_calls],
                            "apiMessages": [dict(item) for item in api_messages],
                        }
                        # A long-running interactive CLI can print an OAuth URL
                        # and then wait. Run the tool in a sibling task so its
                        # ContextVar output sink can feed AG-UI before completion.
                        output_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                        sink_token = set_tool_output_sink(output_queue.put_nowait)
                        try:
                            tool_task = asyncio.create_task(self._run_tool(
                                runtime=runtime,
                                iteration=iteration,
                                call_id=tc["id"],
                                tool_name=tool_name,
                                arguments=raw_arguments,
                            ))
                        finally:
                            # The child task captured the binding; resetting the
                            # parent prevents output leaking into the next call.
                            reset_tool_output_sink(sink_token)

                        output_task: asyncio.Task[dict[str, Any]] | None = None
                        try:
                            while not tool_task.done():
                                output_task = asyncio.create_task(output_queue.get())
                                done, _ = await asyncio.wait(
                                    {tool_task, output_task},
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                if output_task in done:
                                    output = output_task.result()
                                    yield {
                                        "type": "tool_output",
                                        "payload": {"toolCallId": tc["id"], **output},
                                    }
                                else:
                                    output_task.cancel()
                                    await asyncio.gather(output_task, return_exceptions=True)
                        except asyncio.CancelledError:
                            if output_task is not None:
                                output_task.cancel()
                            tool_task.cancel()
                            await asyncio.gather(
                                *(task for task in (tool_task, output_task) if task is not None),
                                return_exceptions=True,
                            )
                            raise
                        while not output_queue.empty():
                            output = output_queue.get_nowait()
                            yield {
                                "type": "tool_output",
                                "payload": {"toolCallId": tc["id"], **output},
                            }
                        tool_result = await tool_task
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
                    notice = self._sandbox_user_notice(
                        context, tool_name, tool_result, config
                    )
                    if notice is not None:
                        yield {"type": "status", "payload": {"message": notice}}
                    # Append tool result to api_messages
                    # 失败结果同样以正常 tool 消息回填。模型据此自行修正后重试，
                    # 这正是工具错误可恢复的关键：异常不冒泡，只变成一次观测。
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })
                yield {"type": "status", "payload": {"message": config.status_model_started}}

            # Reached iteration limit
            limit_message = config.tool_iteration_limit_message
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
                iteration=config.max_model_iterations,
            )
            yield {"type": "thinking", "payload": limit_step}
            result = self._final_state(working_messages, trace, thinking)
            agent_scope.complete(result)
            await agent_scope.__aexit__(None, None, None)
            yield {"type": "trace", "payload": {"entry": trace[-1]}}
            yield {"type": "final", "payload": result}
        except (asyncio.CancelledError, GeneratorExit) as exc:
            # Cancellation must close model/agent observations but must never be
            # converted to a model-visible tool error or an AG-UI success event.
            await agent_scope.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        except Exception as exc:
            # Agent 级异常（模型不可达、上下文构建失败等）不像工具失败那样可恢复，
            # 记录后继续上抛，由 agui 层转成 RUN_ERROR 告知前端。
            await agent_scope.__aexit__(type(exc), exc, exc.__traceback__)
            # MCP discovery and context planning run before the first trace entry
            # exists, so indexing blindly would raise an IndexError that replaces
            # the failure the operator actually needs to see.
            yield {
                "type": "trace",
                "payload": {
                    "entry": trace[-1] if trace else f"agent:failed:{type(exc).__name__}"
                },
            }
            raise

    async def _provider_model_stream(
        self,
        request: ModelCallPayload,
        *,
        client: AsyncOpenAI,
        config: Settings,
        max_output_tokens: int,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Adapt provider chunks to a typed stream without buffering visible deltas."""

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [dict(message) for message in request.messages],
            "stream": True,
            "max_tokens": max_output_tokens,
            "timeout": config.model_request_timeout_seconds,
        }
        if request.tools:
            kwargs["tools"] = [dict(tool) for tool in request.tools]
            kwargs["tool_choice"] = "auto"
        if request.reasoning_effort and request.reasoning_effort != "none":
            kwargs["reasoning_effort"] = request.reasoning_effort

        started_at = time.perf_counter()
        stream = await client.chat.completions.create(**kwargs)
        content_buffer = ""
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        response_id = ""
        async for chunk in self._iter_stream_with_idle_timeout(stream, config):
            if chunk.id:
                response_id = chunk.id
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            reasoning_content = getattr(delta, "reasoning_content", None)
            if reasoning_content:
                yield ModelReasoningDelta(reasoning_content)
            if delta.content:
                content_buffer += delta.content
                yield ModelTextDelta(delta.content)
            # Tool arguments arrive across chunks. Preserve provider indexes and
            # aggregate only the protocol data that has no user-visible delta.
            if delta.tool_calls:
                for tool_delta in delta.tool_calls:
                    index = tool_delta.index
                    target = tool_call_buffers.setdefault(
                        index,
                        {"id": tool_delta.id or "", "name": "", "arguments": ""},
                    )
                    if tool_delta.id:
                        target["id"] = tool_delta.id
                    if tool_delta.function:
                        if tool_delta.function.name:
                            target["name"] = tool_delta.function.name
                        if tool_delta.function.arguments:
                            target["arguments"] += tool_delta.function.arguments

        tool_calls = tuple(
            tool_call_buffers[index] for index in sorted(tool_call_buffers)
        )
        yield ModelCallCompleted(
            ModelResultPayload(
                iteration=request.iteration,
                model=request.model,
                response_id=response_id,
                output_text=content_buffer.strip(),
                function_call_count=len(tool_calls),
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                tool_calls=tool_calls,
                operation_id=request.operation_id,
            )
        )

    async def _iter_stream_with_idle_timeout(
        self,
        stream: Any,
        config: Settings,
    ) -> AsyncIterator[Any]:
        """Yield provider chunks, aborting when the stream stalls mid-response.

        The request-level timeout only bounds the initial response. A provider
        that opens the stream and then goes silent would otherwise hold this run,
        an access-layer concurrency slot, and the session lock indefinitely.
        """

        idle_timeout = config.model_stream_idle_timeout_seconds
        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), idle_timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as exc:
                await self._close_stream(stream)
                raise TimeoutError(
                    f"Model stream stalled for more than {idle_timeout:g}s "
                    "without sending a chunk."
                ) from exc
            yield chunk

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        """Release provider stream resources on the abort path."""

        close = getattr(stream, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Cleanup must not replace the timeout that triggered it.
            pass

    async def _run_tool(
        self,
        runtime: dict[str, Any],
        iteration: int,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """执行单个工具；异常转成模型可见结果，避免整轮 run 中断。"""

        try:
            result = await self._execute_tool(
                runtime=runtime,
                iteration=iteration,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
            )
            return result.output
        except Exception as exc:
            # Tool failures must not abort the whole run. Cancellation and
            # process-level BaseException subclasses still propagate normally.
            return self._recoverable_tool_error(tool_name=tool_name, error=exc)

    async def _execute_tool(
        self,
        runtime: dict[str, Any],
        iteration: int,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        """Resolve one logical call and pass it through the sealed Tool Pipeline."""

        config: Settings = runtime["config"]
        tools: list[ToolDefinition] = runtime["tools"]
        mcp_client_manager: McpClientManager = runtime["mcp_client_manager"]
        skills: list[dict[str, Any]] = runtime["skills"]
        pipeline: AgentPipelineRuntime = runtime["pipeline"]
        context = pipeline.context
        local_tool = next((tool for tool in tools if tool.name == tool_name), None)
        selected_skill = self._selected_skill(tool_name, skills)
        # 本地工具优先。只有名字对不上任何本地工具时，才考虑它是不是
        # provider 把 Skill 名当成函数名直接调用了。
        if local_tool is None and selected_skill is not None:
            local_tool = next(
                (tool for tool in tools if tool.name == "Skill"),
                None,
            )
            if local_tool is not None:
                arguments = self._skill_alias_arguments(tool_name, arguments)
        if local_tool is not None:
            permission_tool_name = local_tool.name
            if permission_tool_name in _READ_ONLY_LOCAL_TOOLS:
                # Older turns or providers can replay the former escalation field.
                # Drop it before validation so a harmless read neither asks for HITL
                # nor fails merely because its schema has since been tightened.
                arguments = {
                    key: value
                    for key, value in arguments.items()
                    if key not in {"sandbox_permissions", "escalation_scope", "escalation_resource"}
                }
            request = ToolCallRequest(
                call_id=call_id or str(uuid.uuid4()),
                iteration=iteration,
                requested_name=tool_name,
                canonical_name=permission_tool_name,
                arguments=arguments,
                source="local",
            )

            async def preflight(current: ToolCallRequest) -> None:
                if context.metadata.get("permission_mode") != "full_access":
                    self._enforce_skill_allowlist(context, current.canonical_name)
                decision = self._local_permission_decision(
                    config,
                    context,
                    current.canonical_name,
                    dict(current.arguments),
                )
                await self._enforce_permission(
                    runtime,
                    current.canonical_name,
                    decision,
                    {
                        "toolName": current.requested_name,
                        "callId": current.call_id,
                        "iteration": current.iteration,
                        "arguments": dict(current.arguments),
                        "source": "local",
                    },
                )

            async def execute(current: ToolCallRequest) -> str:
                resolved = next(
                    (tool for tool in tools if tool.name == current.canonical_name),
                    None,
                )
                if resolved is None:
                    raise RuntimeError(
                        f"Unknown local tool requested: {current.canonical_name}"
                    )
                current_arguments = dict(current.arguments)
                # Validation remains after permission and ToolStarted to preserve
                # the existing security/observability ordering contract.
                validate_tool_arguments(resolved.parameters, current_arguments)
                output = await resolved.execute(current_arguments)
                if current.canonical_name == "Skill":
                    self._activate_skill_allowlist(context, output)
                return output

            return await pipeline.run_tool(
                request,
                preflight=preflight,
                execute=execute,
            )

        target = self._parse_mcp_tool(tool_name)
        # 既不是本地工具也不符合 MCP 命名约定：可能是模型幻觉出的函数名，
        # 直接拒绝执行，错误会作为工具结果回给模型让它改用真实工具。
        if target is None:
            await pipeline.emit_failure(
                RuntimeError(f"Unknown tool requested: {tool_name}"),
                stage="tool_resolve",
                detail={"toolName": tool_name, "callId": call_id},
            )
            raise RuntimeError(f"Unknown tool requested: {tool_name}")

        server_id, name = target
        request = ToolCallRequest(
            call_id=call_id or str(uuid.uuid4()),
            iteration=iteration,
            requested_name=tool_name,
            canonical_name=name,
            arguments=arguments,
            source="mcp",
            server_id=server_id,
        )

        async def preflight(current: ToolCallRequest) -> None:
            if current.server_id is None:
                raise RuntimeError("MCP tool request is missing server_id")
            if context.metadata.get("permission_mode") != "full_access":
                self._enforce_skill_allowlist(context, current.requested_name)
            await self._enforce_permission(
                runtime,
                f"MCP tool {current.server_id}:{current.canonical_name}",
                (
                    PermissionDecision("allow", "full access selected for this run")
                    if context.metadata.get("permission_mode") == "full_access"
                    else check_permission(
                        "mcp", f"{current.server_id}:{current.canonical_name}"
                    )
                ),
                {
                    "toolName": current.canonical_name,
                    "callId": current.call_id,
                    "iteration": current.iteration,
                    "serverId": current.server_id,
                    "arguments": dict(current.arguments),
                    "source": "mcp",
                },
            )

        async def execute(current: ToolCallRequest) -> str:
            if current.server_id is None:
                raise RuntimeError("MCP tool request is missing server_id")
            return await mcp_client_manager.call_tool(
                current.server_id,
                current.canonical_name,
                dict(current.arguments),
            )

        return await pipeline.run_tool(
            request,
            preflight=preflight,
            execute=execute,
        )

    @staticmethod
    def _local_permission_decision(
        config: Settings,
        context: AgentRunContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PermissionDecision:
        """Resolve local-tool policy after middleware has finalized the request."""

        decision = check_permissions(
            tool_name,
            OpenAIAgent._permission_subjects(tool_name, arguments),
        )
        if tool_name in _READ_ONLY_LOCAL_TOOLS and decision.behavior == "ask":
            decision = PermissionDecision(
                "allow", "read-only local access does not require HITL"
            )
        if context.metadata.get("permission_mode") == "full_access":
            return PermissionDecision("allow", "full access selected for this run")
        if (
            tool_name not in _ESCALATABLE_LOCAL_TOOLS
            or arguments.get("sandbox_permissions") != "require_escalated"
        ):
            return decision
        if tool_name != "Bash":
            return PermissionDecision(
                "ask", "该工具请求访问默认沙箱范围之外的本机资源。"
            )

        scope = arguments.get("escalation_scope")
        resource = str(arguments.get("escalation_resource") or "").strip()
        if scope not in {
            "outside_workspace_write",
            "host_resource",
            "network_destination",
        } or not resource:
            return PermissionDecision(
                "deny",
                "Bash escalation requires escalation_scope and a concrete "
                "escalation_resource.",
            )
        if scope == "network_destination" and is_domain_allowed(
            resource, config.bash_sandbox_allowed_domains
        ):
            return PermissionDecision(
                "deny",
                f"Network destination {resource!r} is already allowed by the "
                "Bash sandbox; retry normally or adjust timeout_seconds instead "
                "of requesting escalation.",
            )
        return PermissionDecision(
            "ask",
            (
                f"该命令请求访问网络白名单外的目标：{resource}"
                if scope == "network_destination"
                else f"该命令请求访问默认沙箱外的本机资源：{resource}"
            ),
        )

    async def _enforce_permission(
        self,
        runtime: dict[str, Any],
        target: str,
        decision: PermissionDecision,
        detail: dict[str, Any],
    ) -> None:
        """执行权限：deny 立即失败；ask 经 approval_handler 挂起等人决策。"""

        approved_targets: set[str] = runtime["approved_targets"]
        approval_handler = runtime["approval_handler"]
        resume_authorization = runtime.get("_resume_authorization")
        if isinstance(resume_authorization, dict):
            detail_hash = canonical_json_sha256({
                "target": detail.get("toolName") or target,
                "source": detail.get("source"),
                "serverId": detail.get("serverId"),
                "arguments": detail.get("arguments", detail.get("input", {})),
            })
            if (
                detail.get("callId") == resume_authorization.get("callId")
                and detail_hash == resume_authorization.get("requestHash")
            ):
                # 一次性授权在进入 execute 前消费；同 run 后续相同工具仍需重新判定。
                runtime.pop("_resume_authorization", None)
                return
        if decision.behavior == "allow" or target in approved_targets:
            return
        if decision.behavior == "ask":
            if approval_handler is None:
                raise RuntimeError(f"{target} requires manual approval")
            response = await approval_handler(target, decision, detail)
            if response.get("action") == "approve":
                if response.get("scope") == "run":
                    approved_targets.add(target)
                return
            raise RuntimeError(f"User denied approval for {target}")
        raise RuntimeError(decision.reason or f"Permission denied for {target}")

    @staticmethod
    def _enforce_skill_allowlist(context: AgentRunContext, tool_name: str) -> None:
        """应用已调用 Skill 声明的 allowedTools 白名单。"""

        allowlist = context.skill_allowlist
        if allowlist is None or tool_name in allowlist:
            return
        allowed = ", ".join(sorted(allowlist)) or "none"
        raise RuntimeError(
            f"Skill {context.skill_allowlist_owner} restricts tool use to: {allowed}. "
            f"{tool_name} is not permitted while this skill is active."
        )

    @staticmethod
    def _activate_skill_allowlist(context: AgentRunContext, result: str) -> None:
        """把 Skill 返回的 allowedTools 闩到本 run 剩余生命周期。

        Skill 工具只回指令文本，白名单在 run 结束前一直生效；此前该字段
        若未闩住会对 UI 与模型都形成误导。
        """

        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("success") is False:
            return
        allowed = payload.get("allowedTools")
        if not isinstance(allowed, list) or not allowed:
            return
        # Skill itself stays available so one skill can hand off to another.
        context.skill_allowlist = {str(item) for item in allowed} | {"Skill"}
        context.skill_allowlist_owner = str(payload.get("commandName") or "skill")

    @staticmethod
    def _recoverable_tool_error(*, tool_name: str, error: Exception) -> str:
        """Convert an already-observed ordinary tool failure for model recovery."""

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

        # 无参工具的参数串通常为空，视为空对象而不是解析错误。
        if not raw_arguments:
            return {}
        decoded = json.loads(raw_arguments)
        # 顶层必须是对象：后续按 key 取参数，数组或标量会在更深处引发
        # 难以定位的类型错误。
        if not isinstance(decoded, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        return decoded

    def _sandbox_user_notice(
        self,
        context: AgentRunContext,
        tool_name: str,
        tool_result: str,
        config: Settings,
    ) -> str | None:
        """Surface sandbox install/degrade messages once (install outcomes always)."""

        message = notice_from_tool_result(
            tool_name, tool_result, settings=config
        )
        if message is None:
            return None
        # Install success/failure should always reach the status pill. Unavailable
        # notices are once per run so repeated Bash calls do not spam the UI.
        if tool_name == "InstallSandbox":
            return message
        if context.metadata.get("sandbox_notice_emitted"):
            return None
        context.metadata["sandbox_notice_emitted"] = True
        return message

    @staticmethod
    def _tool_result_failed(result: str) -> bool:
        """Recognize the common structured failure contracts used by tools."""

        # 纯文本结果无法判断成败，一律按成功处理，避免把正常输出误标为错误。
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return False
        # 三种键分别来自不同来源：ok 是本地工具与恢复路径的约定，
        # success 是部分工具的历史写法，isError 来自 MCP 协议。
        return (
            isinstance(payload, dict)
            and (
                payload.get("ok") is False
                or payload.get("success") is False
                or payload.get("isError") is True
            )
        )

    def _selected_skill(
        self,
        tool_name: str,
        skills: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Resolve a direct function name only against this request's Skills."""

        return next(
            (
                skill
                for skill in skills
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
            # 兜底：取第一个非空的其他字段当作参数。别名调用下字段名不可预期，
            # 拿不到就退回空串，交给 Skill 工具自己报缺参，而不是在这里抛异常。
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

    @classmethod
    def _permission_subjects(cls, tool_name: str, arguments: dict[str, Any]) -> list[str]:
        """为不同工具提取可匹配的权限对象，例如 Bash 命令或文件路径。"""
        if tool_name == "Bash":
            command = str(arguments.get("command") or tool_name)
            # 命令串整体和拆分后的每一段都要过规则：否则 `cd /tmp && rm -rf x`
            # 会绕开一条针对 `rm *` 的 deny 规则。
            return [command, *cls._shell_segments(command)]
        if tool_name in {"Read", "Write", "Edit", "Glob", "Grep", "LS", "NotebookEdit"}:
            return [str(arguments.get("file_path") or arguments.get("path") or tool_name)]
        if tool_name == "WebFetch":
            return [str(arguments.get("url") or tool_name)]
        if tool_name == "WebSearch":
            return [str(arguments.get("query") or tool_name)]
        if tool_name == "ReadMcpResourceTool":
            return [
                f"{arguments.get('server_id') or arguments.get('serverId') or ''}"
                f":{arguments.get('uri') or ''}"
            ]
        if tool_name == "Skill":
            return [str(arguments.get("skill") or tool_name)]
        return [tool_name]

    @staticmethod
    def _shell_segments(command: str) -> list[str]:
        """Split a shell command on its chaining operators for rule matching."""

        segments = re.split(r"&&|\|\||;|\||\n", command)
        return [segment.strip() for segment in segments if segment.strip()]

    def _build_tool_specs(
        self,
        mcp_tools: list[McpToolDescriptor],
        tools: list[ToolDefinition],
    ) -> list[dict[str, Any]]:
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
            for tool in tools
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
        # 工具名自身可能含 `__`，所以只切前两段，剩下的原样拼回去。
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
