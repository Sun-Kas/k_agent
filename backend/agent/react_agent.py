"""OpenAI 兼容聊天 API 上的流式 ReAct 主循环（Reason ↔ Act ↔ Observe）。

pipeline 核心：`KAgentRunner` 复用一个 `OpenAIAgent`，每轮调用
`create_runtime` 拼出请求级运行信息，再由同一个 Agent 的 `run_stream_react`
驱动模型推理、工具执行与观察回填，最后交给 `agui` 转 AG-UI。
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
    compose_api_messages as compose_messages,
    estimate_text_tokens,
    prune_old_tool_outputs,
)
from backend.config.config import Settings
from backend.mcp_tool import McpClientManager, McpToolDescriptor
from backend.permissions import PermissionDecision, check_permission, check_permissions
from backend.approvals import canonical_json_sha256
from backend.sandbox import is_domain_allowed, notice_from_tool_result
from backend.tools import ToolDefinition, validate_tool_arguments
from backend.tools.streaming import reset_tool_output_sink, set_tool_output_sink
from backend.user_questions import (
    normalize_user_question_answers,
    normalize_user_questions,
    render_user_question_result,
)


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
                prompt=request.prompt,
                model_config=request.model_config,
                tool_definition_tokens=tool_definition_tokens,
            )
            # compose 后的 messages 含 system 和 provider 协议消息，是唯一驱动
            # ReAct 的列表；会话持久化另由流式事件完成。
            messages = compose_messages(
                context_plan.messages,
                prompt=request.prompt,
                context_summary=context_plan.summary,
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
            "messages": messages,
            "selected_model": selected_model,
            "client": client,
            "loaded_memory_paths": set(
                [
                    *(request.prompt.initial_memory_paths if request.prompt is not None else ()),
                    *request.loaded_memory_paths,
                ]
            ),
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
        """驱动一次 ReAct 循环，直到 Finish、迭代上限、取消或不可恢复错误。

        ReAct（Yao et al.）在本函数里的对应关系：

        - **Reason**：把 ``messages`` 发给模型。产出思考增量、可见文本，以及
          零个或多个 function ``tool_calls``。没有 tool_calls 就是 Finish。
        - **Act**：按模型给出的顺序逐个执行本地/MCP 工具。权限、Skill 白名单、
          HITL 审批都在 ``_run_tool`` 内，本循环只负责发卡和收结果。
        - **Observe**：每次 Act 的字符串结果写成 ``role=tool`` 消息，追加到
          ``messages``，成为下一轮 Reason 的输入。工具失败同样是 Observation：
          异常不冒泡，模型读到错误文案后再 Reason 一次以修正。

        对用户和 Access Layer 来说，真相是本函数 **yield 出去的事件**
        （``message_*`` / ``tool_*``）。Access Layer 按 AG-UI 事件 upsert session；
        ``agui`` 遇到内部 ``final`` 时只会收尾，不会用 payload 覆盖会话。

        ``messages`` 是**唯一喂给模型的列表**：含 system、带
        ``tool_calls`` 的 assistant，以及用 ``tool_call_id`` 配对的 tool。
        对用户可见的消息与工具结果依靠 yield 出去的事件持久化，不在
        Agent 内部再维护第二份 ``ChatMessage`` 快照。

        循环正常出口是「Reason 不再请求工具」。跑满 ``max_model_iterations`` 是兜底，
        会插入上限文案后 Finish，而不是让 run 无限继续。HITL Interrupt 发生在 Act
        边界（``runtime["_react_tool_boundary"]``）；恢复时先把未做完的 Act 做完，
        再从 ``checkpoint.iteration + 1`` 开始下一轮 Reason，避免重放已流式打出的模型输出。

        ``create_runtime`` 已完成 prompt、工具表、上下文预算；这里只驱动执行。
        """

        request: AgentRunRequest = runtime["request"]  # 本轮已拼好的 prompt / 消息 / 权限入参
        config: Settings = runtime["config"]  # 迭代上限、默认模型、状态文案
        trace: list[str] = runtime["trace"]  # 内部轨迹，最终打进 final.payload
        thinking: list[dict[str, Any]] = runtime["thinking"]  # UI 思考步骤，供 thinking 事件
        selected_model: str = runtime["selected_model"]  # 实际发给 provider 的模型 id
        client: AsyncOpenAI = runtime["client"]  # 本轮专用客户端（apiKey / baseUrl）
        context: AgentRunContext = runtime["context"]  # 本轮 run 元数据（含 permission_mode）
        pipeline: AgentPipelineRuntime = runtime["pipeline"]  # Observer / Middleware 管线
        mcp_tools: list[McpToolDescriptor] = runtime["mcp_tools"]  # 本轮已过滤的 MCP 工具
        tool_specs: list[dict[str, Any]] = runtime["tool_specs"]  # chat.completions 的 tools schema
        context_plan = runtime["context_plan"]  # 裁剪 / 摘要后的上下文预算
        # 含 system 的 provider 协议列表，也是 ReAct 循环唯一的消息状态。
        messages: list[dict[str, Any]] = runtime["messages"]
        loaded_memory_paths: set[str] = runtime["loaded_memory_paths"]
        resume_checkpoint = runtime.get("resume_checkpoint")
        if isinstance(resume_checkpoint, dict):
            restored_paths = resume_checkpoint.get("loadedMemoryPaths", [])
            if not isinstance(restored_paths, list) or not all(
                isinstance(item, str) for item in restored_paths
            ):
                raise RuntimeError("K Agent checkpoint has invalid memory state")
            loaded_memory_paths.update(restored_paths)

        # agent_run 把 before_agent / AgentStarted 与 after_agent / AgentCompleted
        # 收口成一对；后面每次 Finish 或失败都必须走 __aexit__，否则观测会漏收尾。
        # AgentStarted 带的是开跑时的压缩窗口，不是全量 session。
        agent_scope = pipeline.agent_run(list(context_plan.messages))
        try:
            await agent_scope.__aenter__()
            await pipeline.emit_context_built(
                ContextPlanPayload(
                    input_message_count=len(request.messages),
                    active_message_count=len(context_plan.messages),
                    provider_message_count=len(messages),
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
            # -----------------------------------------------------------------
            # 开场：还不是 ReAct 一步。把本轮上下文预算、memory/MCP 痕迹和「任务
            # 已读入」思考步骤推给前端，再进入循环。trace[-1] 来自 AgentStarted。
            # -----------------------------------------------------------------
            yield {
                "type": "context_state",
                "payload": {
                    **context_plan.as_dict(),
                    "loadedMemoryPaths": sorted(loaded_memory_paths),
                },
            }
            yield self._run_trace(trace[-1])
            if loaded_memory_paths:
                trace.append(
                    f"memory:eager_loaded:{len(loaded_memory_paths)} files"
                )
                yield self._run_trace(trace[-1])
            if mcp_tools:
                trace.append(f"tools:mcp_catalog:{len(mcp_tools)} tools")
                yield self._run_trace(trace[-1])
            preparation = self._thinking_step(
                thinking,
                phase="analysis",
                title="理解任务",
                detail=f"已读取对话上下文，并准备 {len(tool_specs)} 个可用工具。",
                status="complete",
                iteration=0,
            )
            yield {"type": "thinking", "payload": preparation}
            yield self._run_status(config.status_model_started)

            # -----------------------------------------------------------------
            # 可选：从 HITL 中断点续跑 Act（不重放 Reason）
            #
            # checkpoint 停在某一轮 Reason 之后、某一批 tool_calls 之中。
            # 当时模型输出已经流式给过前端，所以这里只补执行剩余工具，把
            # Observation 写回 messages，然后从 iteration+1 再 Reason。
            # -----------------------------------------------------------------
            start_iteration = 0
            if isinstance(resume_checkpoint, dict):
                if resume_checkpoint.get("kind") != "react_tool_boundary":
                    raise RuntimeError("K Agent checkpoint is not a ReAct tool boundary")
                checkpoint_messages = resume_checkpoint.get("modelMessages")
                # 同一批tool calls
                pending_calls = resume_checkpoint.get("pendingCalls")
                # 审核的是这批 tool calls 中的第几个
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
                # 用中断时的 provider 消息覆盖 create_runtime 拼出的新列表，
                # 否则 assistant.tool_calls 与后续 tool 消息对不上。
                messages = [dict(message) for message in checkpoint_messages]
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
                    tool_executed = False
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
                    if index == pending_index and tool_name == "AskUserQuestion":
                        # A question Resume is an Observation, not a permission grant.
                        # The trusted checkpoint supplies the original form; the
                        # browser contributes only selections and optional text.
                        question_hash = canonical_json_sha256({
                            "target": tool_name,
                            "source": "user_input",
                            "serverId": None,
                            "arguments": raw_arguments,
                        })
                        if question_hash != runtime.get("resume_request_hash"):
                            raise RuntimeError(
                                "AskUserQuestion resume does not match the interrupted call"
                            )
                        if resume_decision.get("status") == "cancelled":
                            tool_result = json.dumps(
                                {"ok": False, "cancelled": True}, ensure_ascii=False
                            )
                        else:
                            if not isinstance(decision_payload, dict):
                                raise RuntimeError("AskUserQuestion resume payload is missing")
                            # 问题定义只信 checkpoint 参数；浏览器只能提交 selected/custom。
                            questions = normalize_user_questions(raw_arguments)
                            answers = normalize_user_question_answers(
                                questions, decision_payload.get("answers")
                            )
                            # 不跑 AskUserQuestion executor；规范化答案就是这次 Observation。
                            tool_result = json.dumps(
                                render_user_question_result(questions, answers),
                                ensure_ascii=False,
                            )
                    elif index == pending_index and not approved:
                        # 用户拒绝也要留下 Observation，否则下一轮 Reason 会以为
                        # 这个 tool_call 还没结果，再次请求同一工具。
                        tool_result = (
                            f"Tool {tool_name} was not executed because the user denied "
                            "or cancelled the approval request."
                        )
                    else:
                        if index == pending_index:
                            # 仅当前被审批的那一次调用带上 resume 授权；同批后续
                            # 工具若仍需 HITL，会再次 Interrupt，而不是一次批过。
                            runtime["_resume_authorization"] = {
                                "callId": call_id,
                                "requestHash": runtime.get("resume_request_hash"),
                            }
                        runtime["_react_tool_boundary"] = {
                            "version": 2,
                            "kind": "react_tool_boundary",
                            "iteration": checkpoint_iteration,
                            "pendingIndex": index,
                            "pendingCalls": [dict(item) for item in pending_calls],
                            "modelMessages": [dict(item) for item in messages],
                            "loadedMemoryPaths": sorted(loaded_memory_paths),
                        }
                        # 批准不作为 _run_tool 参数：密封 preflight 只认 runtime。
                        # _enforce_permission 用 callId + requestHash 消费上面的一次性授权。
                        tool_result = await self._run_tool(
                            runtime=runtime,
                            iteration=checkpoint_iteration,
                            call_id=call_id,
                            tool_name=tool_name,
                            arguments=raw_arguments,
                        )
                        tool_executed = True
                    tool_message_id = str(uuid.uuid4())
                    yield {
                        "type": "tool_result",
                        "payload": {
                            "toolCallId": call_id,
                            "messageId": tool_message_id,
                            "content": tool_result,
                        },
                    }
                    observation = (
                        await self._observation_with_lazy_memory(
                            runtime,
                            call_id=call_id,
                            tool_result=tool_result,
                        )
                        if tool_executed
                        else tool_result
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": observation,
                    })
                # 本轮 Reason 已在旧 run 完成；从下一 iteration 重新 Reason。
                start_iteration = checkpoint_iteration + 1
                yield self._run_status(config.status_model_started)

            # -----------------------------------------------------------------
            # ReAct 主循环
            #
            #   每轮：Observe(裁剪旧工具结果) → Reason(模型) → Finish | Act(工具)
            #   Act 结束后 Observation 已写进 messages，下一轮 for 再 Reason。
            #
            # range 上界是 inclusive：最后一次迭代仍允许一次 Reason。若仍要工具，
            # 循环结束后走上限 Finish，避免「差一轮就能答完」被直接掐掉。
            # -----------------------------------------------------------------
            for iteration in range(start_iteration, config.max_model_iterations + 1):
                # ----- Observe（上下文）-----
                # 工具结果是上下文增长最快的来源。必须在 Reason 之前裁剪：若等
                # 超预算再处理，本轮请求已经发不出去。这里只压缩旧 Observation
                # 正文，不删除 tool 消息本身，以免破坏 tool_call_id 配对。
                before_tool_chars = self._tool_output_chars(messages)
                before_tool_outputs = self._tool_output_contents(messages)
                messages = prune_old_tool_outputs(messages)
                after_tool_chars = self._tool_output_chars(messages)
                after_tool_outputs = self._tool_output_contents(messages)
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

                # ----- Reason -----
                model_request = ModelCallPayload(
                    iteration=iteration,
                    model=selected_model,
                    messages=tuple(dict(message) for message in messages),
                    tools=tuple(tool_specs),
                    reasoning_effort=request.reasoning_effort,
                )
                # provider reasoning 直接映射为 start/delta/end，不再造
                # thinking 快照、写死标题或在 Agent 内累加展示文本。
                # 一次 model call 只有一条 reasoning message，内外层事件共用同一 ID。
                reasoning_id: str | None = None
                # 可见正文的 messageId；第一个 token 就要 message_start，必须先有 id。
                message_id = str(uuid.uuid4())
                # 还没出过可见文字。只思考/只调工具时不要发空的 message_start。
                message_started = False
                # 「正在思考...」只发一次，避免每个 reasoning delta 刷一条 status。
                reasoning_status_sent = False
                # 等管道最后一帧 ModelCallCompleted；没有这一帧就当模型管线失败。
                model_result: ModelResultPayload | None = None

                async def provider_terminal(current: ModelCallPayload):
                    # Middleware wrap_model 的最内层：真正打 provider。
                    # current 可能已被 before_model / wrap_model 改写。
                    async for item in self._model_call_stream(
                        current,
                        client=client,
                        config=config,
                        max_output_tokens=int(
                            request.model_config.get("maxOutputTokens") or 8192
                        ),
                    ):
                        yield item

                # model call stream
                # 一轮 model call 的结束标志是收到 ModelCallCompleted 事件。
                # 期间可能收到 ModelReasoningDelta 和 ModelTextDelta 事件。
                # 最终要么正文输出结束，要么function call
                async for model_event in pipeline.stream_model(
                    model_request,
                    provider_terminal,
                ):
                    if isinstance(model_event, ModelReasoningDelta):
                        if message_started:
                            raise RuntimeError(
                                "Provider emitted reasoning after the text stream started"
                            )
                        if reasoning_id is None:
                            reasoning_id = str(uuid.uuid4())
                            yield {
                                "type": "reasoning_start",
                                "payload": {"reasoningId": reasoning_id},
                            }
                        if not reasoning_status_sent:
                            reasoning_status_sent = True
                            yield self._run_status("正在思考...")
                        yield {
                            "type": "reasoning_delta",
                            "payload": {
                                "reasoningId": reasoning_id,
                                "content": model_event.content,
                            },
                        }

                    elif isinstance(model_event, ModelTextDelta):
                        if not message_started:
                            # 第一个正文 delta 是 AG-UI reasoning/text 的硬边界。
                            # 必须先收口 reasoning，再开始 TEXT_MESSAGE。
                            if reasoning_id is not None:
                                yield {
                                    "type": "reasoning_end",
                                    "payload": {"reasoningId": reasoning_id},
                                }
                                reasoning_id = None
                            message_started = True
                            yield {"type": "message_start", "payload": {"messageId": message_id}}
                        yield {
                            "type": "delta",
                            "payload": {
                                "messageId": message_id,
                                "content": model_event.content,
                            },
                        }
                    elif isinstance(model_event, ModelCallCompleted):
                        # 流必须收到这一帧才算 Reason 完成；tool_calls 在这里聚齐。
                        model_result = model_event.result

                if model_result is None:
                    raise RuntimeError("Model pipeline finished without a result")
                tool_calls = [dict(item) for item in model_result.tool_calls]

                # 没有正文时，Completed 才是 reasoning 的终止边界。
                if reasoning_id is not None:
                    yield {
                        "type": "reasoning_end",
                        "payload": {"reasoningId": reasoning_id},
                    }
                if message_started:
                    yield {"type": "message_end", "payload": {"messageId": message_id}}

                # ----- Finish：Reason 未请求工具，循环结束 -----
                # 可见文本已经 yield 过；完成事件直接携带最终文本。
                if not tool_calls:
                    result = self._final_state(model_result.output_text, trace, thinking)
                    agent_scope.complete(result)
                    await agent_scope.__aexit__(None, None, None)
                    yield self._run_trace(trace[-1])
                    yield {"type": "final", "payload": result}
                    return

                # ----- Act：按顺序执行本轮全部 tool_calls（不并行）-----
                # 并行会打乱 Observation 顺序，也让 HITL checkpoint 的 pendingIndex
                # 失去「做到第几个」的含义。同轮多个工具仍是一次 Reason 的产物。
                #
                # 带 tool_calls 的 assistant 必须进 messages，后续 tool 消息才能配对。
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": model_result.output_text or None,
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
                messages.append(assistant_message)

                for tool_call_index, tc in enumerate(tool_calls):
                    tool_name = tc["name"]
                    tool_executed = False
                    argument_error: Exception | None = None
                    try:
                        raw_arguments = self._decode_tool_arguments(tc["arguments"])
                    except Exception as exc:
                        # 模型生成的 JSON 坏掉是可恢复 Observation：把原因写进
                        # tool 结果，下一轮 Reason 才能改参数，而不是整 run 失败。
                        raw_arguments = {}
                        argument_error = exc
                    # 工具卡片只认 tool_start / tool_result；不要再发 phase=tool
                    # 的 thinking，agui 本来就会丢掉，前端也会从 thinking 列表滤掉。
                    yield {
                        "type": "tool_start",
                        "payload": {
                            "toolCallId": tc["id"],
                            "toolCallName": tool_name,
                            "arguments": tc["arguments"] or "{}",
                        },
                    }
                    yield self._run_status(f"调用工具 {tool_name}")
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
                        # 执行前先拍 ReAct 工具边界。权限/HITL 在 _run_tool 里，
                        # 一旦 Interrupt，当前 run 结束；用户同意后是新 run，
                        # 不能重放已流式打出的 Reason，只能从「第几个工具」续 Act。
                        # KAgentRunner.request_approval 会把这份 dict 拷进 checkpoint：
                        # - pendingIndex：本批 tool_calls 里正要执行的下标
                        # - pendingCalls：这一轮模型点名的全部工具
                        # - modelMessages：已含 assistant.tool_calls，不含本工具及之后的 Observation
                        runtime["_react_tool_boundary"] = {
                            "version": 2,
                            "kind": "react_tool_boundary",
                            "iteration": iteration,
                            "pendingIndex": tool_call_index,
                            "pendingCalls": [dict(item) for item in tool_calls],
                            "modelMessages": [dict(item) for item in messages],
                            "loadedMemoryPaths": sorted(loaded_memory_paths),
                        }
                        # 在兄弟任务里跑工具，把过程中的 stdout 先 yield 成 tool_output
                        # （例如 CLI 打出 OAuth URL 再阻塞）。全部结束后 live_result[0]
                        # 才是写入 messages 的 Observation 正文。
                        live_result: list[str] = []
                        async for event in self._tool_excute_serially(
                            runtime,
                            iteration=iteration,
                            call_id=tc["id"],
                            tool_name=tool_name,
                            arguments=raw_arguments,
                            result_out=live_result,
                        ):
                            yield event
                        tool_result = live_result[0]
                        tool_executed = True

                    # ----- Observe（本工具）-----
                    # yield tool_result：用户和 Access Layer 看到的结果。
                    # messages.append：下一轮 Reason 要读的 Observation。
                    tool_message_id = str(uuid.uuid4())
                    yield {
                        "type": "tool_result",
                        "payload": {
                            "toolCallId": tc["id"],
                            "messageId": tool_message_id,
                            "content": tool_result,
                        },
                    }
                    yield self._run_trace(trace[-1], output=tool_result)
                    notice = self._sandbox_user_notice(
                        context, tool_name, tool_result, config
                    )
                    if notice is not None:
                        yield self._run_status(notice)
                    observation = (
                        await self._observation_with_lazy_memory(
                            runtime,
                            call_id=tc["id"],
                            tool_result=tool_result,
                        )
                        if tool_executed
                        else tool_result
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": observation,
                    })
                # 本轮 Act 全部 Observe 完毕；status 切回「正在调用模型」，进入下一 Reason。
                yield self._run_status(config.status_model_started)

            # ----- Finish：迭代上限（兜底，不是模型主动结束）-----
            # 最后一轮 Reason 仍请求了工具，但不再 Act，以免无限转。上限文案
            # 既流式发给用户，也作为明确 output 交给完成 Observer。
            limit_message = config.tool_iteration_limit_message
            assistant_message_id = str(uuid.uuid4())
            yield {"type": "message_start", "payload": {"messageId": assistant_message_id}}
            yield {"type": "delta", "payload": {"messageId": assistant_message_id, "content": limit_message}}
            yield {"type": "message_end", "payload": {"messageId": assistant_message_id}}
            limit_step = self._thinking_step(
                thinking,
                phase="complete",
                title="达到执行上限",
                detail="已停止继续调用工具并返回当前结果。",
                status="complete",
                iteration=config.max_model_iterations,
            )
            yield {"type": "thinking", "payload": limit_step}
            result = self._final_state(limit_message, trace, thinking)
            agent_scope.complete(result)
            await agent_scope.__aexit__(None, None, None)
            yield self._run_trace(trace[-1])
            yield {"type": "final", "payload": result}
        except (asyncio.CancelledError, GeneratorExit) as exc:
            # 取消必须关掉模型/Agent 观测，但绝不能变成模型可见的 tool 错误，
            # 也不能伪装成 AG-UI 成功 Finish。
            await agent_scope.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        except Exception as exc:
            # Agent 级异常（模型不可达、上下文构建失败等）不像工具失败那样可恢复。
            # 记入观测后继续上抛，由 agui 转成 RUN_ERROR。
            await agent_scope.__aexit__(type(exc), exc, exc.__traceback__)
            # 循环前失败时 trace 可能仍为空，不能盲取 trace[-1] 盖掉真正错误。
            yield self._run_trace(
                trace[-1] if trace else f"agent:failed:{type(exc).__name__}"
            )
            raise

    async def _tool_excute_serially(
        self,
        runtime: dict[str, Any],
        *,
        iteration: int,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result_out: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        """跑完当前这一个工具，并把过程 stdout 先 yield 成 ``tool_output``。

        多个 tool_calls 的串行由外层 for 保证：这里不会并行跑两个工具。
        不能直接 ``await _run_tool``：有的 CLI 会先打印 OAuth URL 再阻塞，
        必须在进程结束前把那一行推给前端。

        权限 / HITL 仍在 ``_run_tool`` 内。Interrupt 发生在真正执行前时，
        这里会带着异常退出，通常走不到 live output。

        async generator 不好直接 return 正文，所以把最终字符串写入
        ``result_out[0]``，供调用方写成 Observation。
        """

        # 工具代码通过 ContextVar sink 往这里塞行。create_task 会拷贝「当时」
        # 的 ContextVar 到子任务，所以父任务可以立刻 reset，避免下一个工具串台。
        output_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        sink_token = set_tool_output_sink(output_queue.put_nowait)
        try:
            tool_task = asyncio.create_task(
                self._run_tool(
                    runtime=runtime,
                    iteration=iteration,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )
        finally:
            reset_tool_output_sink(sink_token)

        output_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            # 同时盯「工具结束」和「来了一行输出」，谁先到处理谁。
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
                        "payload": {"toolCallId": call_id, **output},
                    }
                else:
                    # 工具先结束：取消空等 queue.get() 的 task。
                    output_task.cancel()
                    await asyncio.gather(output_task, return_exceptions=True)
        except asyncio.CancelledError:
            # 上层取消 run 时，输出等待和 Bash 都要停，避免工具在后台继续跑。
            if output_task is not None:
                output_task.cancel()
            tool_task.cancel()
            await asyncio.gather(
                *(task for task in (tool_task, output_task) if task is not None),
                return_exceptions=True,
            )
            raise
        # 工具结束后队列里可能还剩几行，排干再取最终结果。
        while not output_queue.empty():
            output = output_queue.get_nowait()
            yield {
                "type": "tool_output",
                "payload": {"toolCallId": call_id, **output},
            }
        result_out.append(await tool_task)

    async def _model_call_stream(
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
            # Preserve the sealed pipeline's final canonical request. Lazy
            # context must never trust pre-middleware names or arguments.
            runtime.setdefault("_completed_tool_requests", {})[call_id] = {
                "name": result.request.canonical_name,
                "arguments": dict(result.request.arguments),
                "source": result.request.source,
            }
            return result.output
        except Exception as exc:
            # Tool failures must not abort the whole run. Cancellation and
            # process-level BaseException subclasses still propagate normally.
            return self._recoverable_tool_error(tool_name=tool_name, error=exc)

    async def _observation_with_lazy_memory(
        self,
        runtime: dict[str, Any],
        *,
        call_id: str,
        tool_result: str,
    ) -> str:
        """Append newly applicable rules only to the model's Observation.

        The browser receives the raw tool result before this method runs. Only
        declared path arguments from local filesystem tools can cause reads;
        arbitrary tool/MCP output is never scanned for paths.
        """

        enricher = runtime.get("observation_enricher")
        if enricher is None:
            return tool_result
        completed = runtime.get("_completed_tool_requests", {}).pop(call_id, None)
        if not isinstance(completed, dict) or completed.get("source") != "local":
            return tool_result
        canonical_name = completed.get("name")
        canonical_arguments = completed.get("arguments")
        if not isinstance(canonical_name, str) or not isinstance(canonical_arguments, dict):
            return tool_result
        return await enricher(canonical_name, canonical_arguments, tool_result)

    async def _execute_tool(
        self,
        runtime: dict[str, Any],
        iteration: int,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        """解析一次工具调用，并送进密封的 Tool Pipeline。

        和 ``_run_tool`` 的分工：这里负责「这是哪个工具、怎么跑」；
        ``_run_tool`` 负责把异常收成模型可见的 Observation。
        ``pipeline.run_tool`` 的顺序是 wrap → preflight（权限/HITL）→
        观测 → execute。Middleware 改参数也会再过 preflight，绕不开安全门。

        解析顺序：同名本地工具 → Skill 别名（模型把 Skill 名当函数名）→
        MCP（``server__tool`` 约定）→ 未知则失败，让模型改用真实工具。
        """

        config: Settings = runtime["config"]  # 权限规则、超时等进程配置
        tools: list[ToolDefinition] = runtime["tools"]  # 本轮本地工具表（含 Skill 元工具）
        mcp_client_manager: McpClientManager = runtime["mcp_client_manager"]  # 已连接的 MCP
        skills: list[dict[str, Any]] = runtime["skills"]  # 本轮勾选的 Skill 列表
        pipeline: AgentPipelineRuntime = runtime["pipeline"]  # wrap / 观测 / 密封执行
        context = pipeline.context  # 本轮 run 元数据（permission_mode、Skill 白名单）
        # 按模型给的名字在本地表里找第一个定义；找不到就是 None。
        local_tool = next((tool for tool in tools if tool.name == tool_name), None)
        
        
        # 正常调用是函数名 Skill，参数里带 skill/args。下面是兼容垫片：
        # 部分模型/网关会把 Skill 名直接当成 tool name。必须先确认它真是
        # 本轮已选 Skill，否则幻觉名不能改道去跑 Skill。
        selected_skill = next(
            (
                skill
                for skill in skills
                if skill.get("enabled", True)
                and tool_name in {
                    str(skill.get("id") or ""),
                    str(skill.get("name") or ""),
                }
            ),
            None,
        )
        if local_tool is None and selected_skill is not None:
            # 拧回标准路径：仍执行本地名为 Skill 的元工具，不把 Skill 名当新工具。
            local_tool = next(
                (tool for tool in tools if tool.name == "Skill"),
                None,
            )
            if local_tool is not None:
                # 把畸形参数收成 {skill, args}，和正规 Skill 调用一致。
                arguments = self._skill_alias_arguments(tool_name, arguments)





        # 正常路径：工具表里找，能匹配上就跑。
        if local_tool is not None:
            # 权限按规范名（Read / Skill），不是模型乱起的别名。
            request = ToolCallRequest(
                call_id=call_id or str(uuid.uuid4()),  # 模型没给 id 时自己造
                iteration=iteration,  # 第几轮 Reason 之后的 Act
                requested_name=tool_name,  # 模型原始函数名
                canonical_name=local_tool.name,  # 真正要执行/鉴权的名字
                arguments=arguments,  # 可能已被 Skill 别名改写
                source="local",  # 与 MCP 路径分流
            )

            async def preflight(current: ToolCallRequest) -> None:
                # wrap 改过参数也会再进这里，不能跳过。
                # 除非权限模式是全开，否则校验 tool 是否在skill的白名单内。
                # TODO: Skill 退出机制，因为用了skill后，后续会进行别的任务，不能一直用skill的白名单进行规范
                if context.metadata.get("permission_mode") != "full_access":
                    # 非全开时，Skill 收窄后的白名单仍然生效。
                    self._enforce_skill_allowlist(context, current.canonical_name)
                if current.canonical_name == "AskUserQuestion":
                    # Unlike permission approvals, user questions are required
                    # even in full-access mode. Validate the form before making
                    # a durable card so malformed model output remains a normal,
                    # recoverable tool error instead of an unanswerable Interrupt.
                    resolved = next(
                        (tool for tool in tools if tool.name == current.canonical_name),
                        None,
                    )
                    if resolved is None:
                        raise RuntimeError("AskUserQuestion tool is unavailable")
                    current_arguments = dict(current.arguments)
                    validate_tool_arguments(resolved.parameters, current_arguments)
                    questions = normalize_user_questions(current_arguments)
                    approval_handler = runtime.get("approval_handler")
                    if approval_handler is None:
                        raise RuntimeError("AskUserQuestion requires an interactive client")
                    # 返回值故意丢掉。Broker.request 只结束本轮 HTTP，占位
                    # {"action":"cancel"} 不是用户答案；答案在 Resume 时写入 Observation。
                    # 若按 _enforce_permission 去读这个 cancel，会把「等人」误判成「已拒绝」。
                    await approval_handler(
                        "AskUserQuestion",
                        PermissionDecision("ask", "需要用户提供信息后才能继续。"),
                        {
                            "toolName": current.requested_name,
                            "callId": current.call_id,
                            "iteration": current.iteration,
                            "arguments": current_arguments,
                            "questions": questions,
                            "source": "user_input",
                        },
                    )
                    # 正常路径执行不到这里：request() 会拆掉原 Run。
                    # 同步返回时 fail-closed，避免落到不可达 executor 伪造答案。
                    raise RuntimeError("AskUserQuestion interrupt closed unexpectedly")
                # 按规则算出 allow / deny / ask（HITL）。
                decision = self._local_permission_decision(
                    config,
                    context,
                    current.canonical_name,
                    dict(current.arguments),  # 拷一份，避免规则侧改 MappingProxy
                )
                # deny 抛错；ask 走审批 Interrupt。
                # Resume 时若 runtime["_resume_authorization"] 匹配本次 call，这里直接放行。
                await self._enforce_permission(
                    runtime,
                    current.canonical_name,
                    decision,
                    {
                        "toolName": current.requested_name,  # 卡片上显示模型点的名
                        "callId": current.call_id,
                        "iteration": current.iteration,
                        "arguments": dict(current.arguments),
                        "source": "local",
                    },
                )

            async def execute(current: ToolCallRequest) -> str:
                # 再查一次表：middleware 可能把 canonical_name 改成别的本地工具。
                resolved = next(
                    (tool for tool in tools if tool.name == current.canonical_name),
                    None,
                )
                if resolved is None:
                    raise RuntimeError(
                        f"Unknown local tool requested: {current.canonical_name}"
                    )
                current_arguments = dict(current.arguments)  # execute 要可变 dict
                # 校验放在权限和 ToolStarted 之后，保持现有顺序。
                validate_tool_arguments(resolved.parameters, current_arguments)
                output = await resolved.execute(current_arguments)  # 真正跑本地实现
                if current.canonical_name == "Skill":
                    # Skill 正文可能声明后续只允许哪些工具。
                    self._activate_skill_allowlist(context, output)
                return output

            # 密封门：wrap → preflight → 观测 → execute。
            return await pipeline.run_tool(
                request,
                preflight=preflight,
                execute=execute,
            )

        # 本地没中：看是不是 MCP 的 server__tool 命名。
        target = self._parse_mcp_tool(tool_name)
        if target is None:
            # 模型幻觉的函数名：观测一条失败，再抛给 _run_tool 收成 Observation。
            await pipeline.emit_failure(
                RuntimeError(f"Unknown tool requested: {tool_name}"),
                stage="tool_resolve",
                detail={"toolName": tool_name, "callId": call_id},
            )
            raise RuntimeError(f"Unknown tool requested: {tool_name}")

        server_id, name = target  # MCP server id 与该 server 上的工具名
        request = ToolCallRequest(
            call_id=call_id or str(uuid.uuid4()),
            iteration=iteration,
            requested_name=tool_name,  # 原始拼接名，白名单按这个匹配
            canonical_name=name,  # MCP 侧真正的 tool name
            arguments=arguments,
            source="mcp",
            server_id=server_id,
        )

        async def preflight(current: ToolCallRequest) -> None:
            if current.server_id is None:
                raise RuntimeError("MCP tool request is missing server_id")
            if context.metadata.get("permission_mode") != "full_access":
                # MCP 白名单用 requested_name（带 server 前缀的那个）。
                self._enforce_skill_allowlist(context, current.requested_name)
            await self._enforce_permission(
                runtime,
                f"MCP tool {current.server_id}:{current.canonical_name}",
                (
                    # 本轮选了全开：不再问 MCP 规则。
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
            # 经连接池调对应 server 上的工具。
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
        """本地工具的 allow / deny / ask，只出结论，不挂起、不执行。

        调用点是 ``_execute_tool`` 的 preflight：middleware wrap 之后、
        ``_enforce_permission`` 之前。``ask`` 才会进 HITL；``deny`` 变成可恢复
        工具错误；``allow`` 才进入参数校验与 execute。

        分层（后者可覆盖前者，但 full_access 直接短路）：
        1. 规则文件 ``check_permissions``：按工具名 + subject（命令/路径/URL）
           取最严结果。
        2. 只读本地工具（Read/Glob/Grep/LS）把 ``ask`` 降成 ``allow``，避免
           读工作区也弹审批。``deny`` 仍生效。
        3. 本 run ``permission_mode == full_access``：一律 ``allow``，包括规则
           deny。这是会话级开关，不是单次工具授权。
        4. 默认可写工具若声明 ``sandbox_permissions=require_escalated``，强制
           ``ask``。模型不能靠这个字段自批；只是申请越权。Bash 还必须带合法
           ``escalation_scope`` + 具体 ``escalation_resource``；已在沙箱网络
           白名单里的域名直接 deny，避免无意义审批。
        """

        # 1. 规则：Bash 会拿整句 + &&/||/; 分段一起匹配，防止链式绕过 deny。
        decision = check_permissions(
            tool_name,
            OpenAIAgent._permission_subjects(tool_name, arguments),
        )
        # 2. 只读探查不走 HITL；规则 deny 不能在这里被抹掉。
        # 只读工具除非明确deny，ask的情况不用管，直接开放就行
        if tool_name in _READ_ONLY_LOCAL_TOOLS and decision.behavior == "ask":
            decision = PermissionDecision(
                "allow", "read-only local access does not require HITL"
            )
        # 3. 全开：跳过规则与越权申请，后面的 sandbox 实现仍可能因 ContextVar 跳过 srt。
        if context.metadata.get("permission_mode") == "full_access":
            return PermissionDecision("allow", "full access selected for this run")
        # 未申请越权，或工具根本不能越权（Skill、WebFetch 等）：采用上面的规则结果。
        if (
            tool_name not in _ESCALATABLE_LOCAL_TOOLS
            or arguments.get("sandbox_permissions") != "require_escalated"
        ):
            return decision
        # 4. 正向越权：Write/Edit/NotebookEdit 只问一次「出沙箱」；细节在卡片参数里。
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
        # 白名单内域名本就可以在沙箱里访问，再申请越权视为模型误用，直接拒绝。
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
        """执行权限：deny 立即失败；ask 经 approval_handler 挂起等人决策。

        Resume 批准写在 runtime["_resume_authorization"]，不另开函数参数，
        避免 wrap/preflight 伪造「用户已批」。匹配 callId + 现算 hash 才放行。
        """

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

    @staticmethod
    def _run_status(message: str) -> dict[str, Any]:
        """顶栏胶囊 / 轨迹摘要的瞬时文案，不是会话 transcript。

        agui 把它打成 CUSTOM ``status``；前端 ``setStatus``，不进消息流、
        也不落工具卡片。和 ``tool_start`` / ``message_*`` 形状相近，所以
        单独成函数，避免在 ReAct 循环里看起来像对话事件。
        """

        return {"type": "status", "payload": {"message": message}}

    @staticmethod
    def _run_trace(entry: str, *, output: str | None = None) -> dict[str, Any]:
        """右侧「执行轨迹」的一行，不是会话 transcript。

        agui 打成 CUSTOM ``trace``；前端只读 ``entry`` 追加到列表。
        ``output`` 仅工具完成时附带，当前 UI 不用。
        """

        payload: dict[str, Any] = {"entry": entry}
        if output is not None:
            payload["output"] = output
        return {"type": "trace", "payload": payload}

    def _final_state(
        self,
        output: str,
        trace: list[str],
        thinking: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """组装内部 final；会话消息由流式事件单独持久化。"""
        return {
            "output": output,
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
