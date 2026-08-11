"""OpenAI 兼容聊天 API 上的流式 React 主循环（模型 ↔ 工具交替）。

pipeline 核心：`KAgentRunner` 交给本模块；产出 thinking/message/tool/trace/final
内部事件，再由 `agui` 转 AG-UI。可变状态全部放在 `run_stream` 局部变量，
实例可并发服务多轮而不串味。
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
from backend.permissions import PermissionDecision, check_permission, check_permissions
from backend.api.schemas import ChatMessage, ChatMeta, ChatRole
from backend.sandbox import is_domain_allowed, notice_from_tool_result
from backend.tools import ToolDefinition, validate_tool_arguments


class OpenAIAgent:
    """有界模型/工具循环；发出与传输无关的内部 run 事件。"""

    def __init__(
        self,
        tools: list[ToolDefinition],
        mcp_client_manager: McpClientManager,
        callbacks: list[Any] | None = None,
        config: Settings | None = None,
        skills: list[dict[str, Any]] | None = None,
        approval_handler: Callable[
            [str, PermissionDecision, dict[str, Any]],
            Awaitable[dict[str, Any]],
        ] | None = None,
    ):
        """注入本轮工具、MCP manager、回调与审批钩子；不持有会话历史。"""
        if not config:
            config = Settings()
        self.config = config
        self.tools = tools
        self.mcp_client_manager = mcp_client_manager
        self.callbacks = callbacks or []
        # 只有本轮选中的 Skill 别名才能解析到规范 Skill 工具；未知函数名一律失败关闭。
        self.skills = list(skills or [])
        self.approval_handler = approval_handler
        # 「本次会话记住」只覆盖本 Agent run 剩余时间；持久权限变更仍走规则文件。
        self._approved_targets: set[str] = set()

    async def run(
        self,
        request: AgentRunRequest,
    ) -> dict[str, Any]:
        """消费流式 run，只返回最终 `final` 载荷（非流式调用方用）。"""

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
        """主循环：上下文预算 → 模型调用 → 权限/工具 → 直到无工具或达迭代上限。"""

        # 本次 run 的全部可变状态都是这里的局部变量，不挂在 self 上：
        # 同一个 OpenAIAgent 实例可能被并发调用，实例级状态会导致会话串味。
        trace: list[str] = []
        thinking: list[dict[str, Any]] = []
        selected_model = request.model_config.get("model", self.config.openai_model)
        # 凭据优先取本次请求选中的模型档案，缺失时才回落到进程级默认配置。
        client = AsyncOpenAI(
            api_key=request.model_config.get("apiKey") or self.config.openai_api_key,
            base_url=request.model_config.get("baseUrl") or self.config.openai_base_url,
        )
        context = AgentRunContext()
        context.metadata["permission_mode"] = request.permission_mode
        callbacks = CallbackManager([TraceCallback(trace), *self.callbacks])

        try:
            mcp_tools = await self.mcp_client_manager.list_tools()
            # 未选中的 MCP server 的工具不能进入 tool specs，否则模型可能调用
            # 用户本次并未授权的服务。空集合表示不限制（沿用 manager 已连接的全部）。
            if request.mcp_server_ids:
                mcp_tools = [
                    tool for tool in mcp_tools
                    if tool.server_id in request.mcp_server_ids
                ]
            tool_specs = self._build_tool_specs(mcp_tools)
            # 工具定义会随每次请求发给模型，属于不可裁剪的固定开销，
            # 必须计入预算，否则工具一多就会在 provider 侧超长。
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
            # working_messages 是要回传给 Access Layer 持久化的会话消息，
            # api_messages 是发给 provider 的载荷（含 system、上下文提醒、tool_calls）。
            # 两者刻意分开：持久化不应污染成 provider 的私有格式。
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

            # 循环的正常出口是下面「模型不再请求工具」的分支；跑满迭代次数属于
            # 异常兜底，会在循环后返回一条上限提示，而不是让 run 无限继续。
            for iteration in range(self.config.max_model_iterations + 1):
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
                    "timeout": self.config.model_request_timeout_seconds,
                }
                # 没有可用工具时连 tools 字段都不传：部分 OpenAI 兼容服务
                # 收到空数组会直接报错。
                if tool_specs:
                    kwargs["tools"] = tool_specs
                    kwargs["tool_choice"] = "auto"
                # 同理，reasoning_effort 只在模型确实支持且用户选了非 none 时下发。
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

                async for chunk in self._iter_stream_with_idle_timeout(stream):
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
                    # 工具调用是分片到达的：id 和函数名通常只在第一个分片里出现，
                    # 参数 JSON 则跨多个分片拼接。必须按 index 归档累积，
                    # 攒齐整个流之后才能解析，中途的参数串是不完整的 JSON。
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
                    notice = self._sandbox_user_notice(context, tool_name, tool_result)
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
            # Agent 级异常（模型不可达、上下文构建失败等）不像工具失败那样可恢复，
            # 记录后继续上抛，由 agui 层转成 RUN_ERROR 告知前端。
            await callbacks.on_error(context, AgentErrorPayload(error=exc, stage="agent_run"))
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

    async def _iter_stream_with_idle_timeout(self, stream: Any) -> AsyncIterator[Any]:
        """Yield provider chunks, aborting when the stream stalls mid-response.

        The request-level timeout only bounds the initial response. A provider
        that opens the stream and then goes silent would otherwise hold this run,
        an access-layer concurrency slot, and the session lock indefinitely.
        """

        idle_timeout = self.config.model_stream_idle_timeout_seconds
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
        callbacks: CallbackManager,
        context: AgentRunContext,
        iteration: int,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """执行单个工具；异常转成模型可见结果，避免整轮 run 中断。"""

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
        """执行已校验的本地或 MCP 工具；调用方负责可恢复错误包装。"""

        local_tool = next((tool for tool in self.tools if tool.name == tool_name), None)
        selected_skill = self._selected_skill(tool_name)
        # 本地工具优先。只有名字对不上任何本地工具时，才考虑它是不是
        # provider 把 Skill 名当成函数名直接调用了。
        if local_tool is None and selected_skill is not None:
            local_tool = next(
                (tool for tool in self.tools if tool.name == "Skill"),
                None,
            )
            if local_tool is not None:
                arguments = self._skill_alias_arguments(tool_name, arguments)
        if local_tool is not None:
            permission_tool_name = local_tool.name
            if context.metadata.get("permission_mode") != "full_access":
                self._enforce_skill_allowlist(context, permission_tool_name)
            decision = check_permissions(
                permission_tool_name,
                self._permission_subjects(permission_tool_name, arguments),
            )
            if context.metadata.get("permission_mode") == "full_access":
                decision = PermissionDecision("allow", "full access selected for this run")
            elif (
                context.metadata.get("permission_mode") != "full_access"
                and arguments.get("sandbox_permissions") == "require_escalated"
            ):
                # Bash escalation is narrower than "run without srt for any
                # reason". Network requests are checked against the effective
                # allowlist so only a new destination can reach HITL; transport
                # failures on an existing destination stay ordinary failures.
                if permission_tool_name == "Bash":
                    scope = arguments.get("escalation_scope")
                    resource = str(arguments.get("escalation_resource") or "").strip()
                    valid_scope = scope in {
                        "outside_workspace_write",
                        "host_resource",
                        "network_destination",
                    }
                    if not valid_scope or not resource:
                        decision = PermissionDecision(
                            "deny",
                            "Bash escalation requires escalation_scope and a concrete "
                            "escalation_resource.",
                        )
                    elif scope == "network_destination" and is_domain_allowed(
                        resource, self.config.bash_sandbox_allowed_domains
                    ):
                        decision = PermissionDecision(
                            "deny",
                            f"Network destination {resource!r} is already allowed by the "
                            "Bash sandbox; retry normally or adjust timeout_seconds "
                            "instead of requesting escalation.",
                        )
                    else:
                        decision = PermissionDecision(
                            "ask",
                            (
                                f"该命令请求访问网络白名单外的目标：{resource}"
                                if scope == "network_destination"
                                else f"该命令请求访问默认沙箱外的本机资源：{resource}"
                            ),
                        )
                else:
                    decision = PermissionDecision(
                        "ask",
                        "该工具请求访问默认沙箱范围之外的本机资源。",
                    )
            await self._enforce_permission(
                permission_tool_name,
                decision,
                {"toolName": tool_name, "arguments": arguments, "source": "local"},
            )
            before_payload = ToolCallPayload(
                iteration=iteration,
                name=tool_name,
                arguments=arguments,
                source="local",
            )
            await callbacks.before_tool(context, before_payload)
            # 校验放在权限之后、执行之前：先确认这次调用被允许，再检查参数合法性，
            # 避免为一次注定被拒的调用暴露参数结构细节。
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
            if permission_tool_name == "Skill":
                self._activate_skill_allowlist(context, result)
            return result

        target = self._parse_mcp_tool(tool_name)
        # 既不是本地工具也不符合 MCP 命名约定：可能是模型幻觉出的函数名，
        # 直接拒绝执行，错误会作为工具结果回给模型让它改用真实工具。
        if target is None:
            raise RuntimeError(f"Unknown tool requested: {tool_name}")

        server_id, name = target
        if context.metadata.get("permission_mode") != "full_access":
            self._enforce_skill_allowlist(context, tool_name)
        await self._enforce_permission(
            f"MCP tool {server_id}:{name}",
            (
                PermissionDecision("allow", "full access selected for this run")
                if context.metadata.get("permission_mode") == "full_access"
                else check_permission("mcp", f"{server_id}:{name}")
            ),
            {
                "toolName": name,
                "serverId": server_id,
                "arguments": arguments,
                "source": "mcp",
            },
        )
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

    async def _enforce_permission(
        self,
        target: str,
        decision: PermissionDecision,
        detail: dict[str, Any],
    ) -> None:
        """执行权限：deny 立即失败；ask 经 approval_handler 挂起等人决策。"""

        if decision.behavior == "allow" or target in self._approved_targets:
            return
        if decision.behavior == "ask":
            if self.approval_handler is None:
                raise RuntimeError(f"{target} requires manual approval")
            response = await self.approval_handler(target, decision, detail)
            if response.get("action") == "approve":
                if response.get("remember"):
                    self._approved_targets.add(target)
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
    ) -> str | None:
        """Surface sandbox install/degrade messages once (install outcomes always)."""

        message = notice_from_tool_result(
            tool_name, tool_result, settings=self.config
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
