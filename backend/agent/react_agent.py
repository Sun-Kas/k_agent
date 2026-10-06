"""OpenAI 兼容聊天 API 上的流式 ReAct 主循环（Reason ↔ Act ↔ Observe）。

pipeline 核心：`KAgentRunner` 复用一个 `OpenAIAgent`，每轮调用
`create_runtime` 拼出请求级运行信息，再由同一个 Agent 的 `run_stream_react`
驱动模型推理、工具执行与观察回填，最后交给 `agui` 转 AG-UI。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
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
    TraceObserver,
)
from backend.agent.contracts import AgentRunRequest
from backend.context import (
    CompactError,
    build_context_plan,
    calculate_context_budget,
    compose_api_messages as compose_messages,
    estimate_message_tokens,
    estimate_text_tokens,
    generate_compaction,
    is_context_length_error,
    limit_tool_result,
    microcompact,
    sanitize_provider_messages,
)
from backend.config.config import Settings
from backend.logging_config import log_event
from backend.permissions import PermissionDecision
from backend.tools.toolset import RequestToolSet
from backend.tools.dispatcher import ToolDispatcher
from backend.tools.contracts import ProviderToolCall, ToolContextPolicy
from backend.tools.streaming import reset_tool_output_sink, set_tool_output_sink


logger = logging.getLogger("k_agent.agent")

def _merge_tool_replacements(existing: object, pending: Any) -> list[dict[str, Any]]:
    """按消息 ID 合并 replacement，避免 full compact 丢掉已提交旧 patch。"""

    merged: dict[str, dict[str, Any]] = {}
    for item in [*(existing if isinstance(existing, list) else []), *list(pending)]:
        if isinstance(item, dict) and isinstance(item.get("messageId"), str):
            merged[item["messageId"]] = dict(item)
    return list(merged.values())


class OpenAIAgent:
    """进程内无状态 Agent；请求状态只存在于 runtime 字典与函数局部变量。"""

    __slots__ = ()

    async def create_runtime(
        self,
        request: AgentRunRequest,
        tools: RequestToolSet,
        dispatcher: ToolDispatcher,
        observers: list[Any] | None = None,
        pipeline_definition: AgentPipelineDefinition | None = None,
        run_context: AgentRunContext | None = None,
        config: Settings | None = None,
        approval_handler: Callable[
            [str, PermissionDecision, dict[str, Any]],
            Awaitable[dict[str, Any]],
        ]
        | None = None,
    ) -> dict[str, Any]:
        """准备模型循环需要的请求级数据；不执行模型或工具循环。"""

        runtime_config = config or Settings()
        tool_set = tools
        trace: list[str] = []
        thinking: list[dict[str, Any]] = []
        context = run_context or AgentRunContext()
        context.metadata["permission_mode"] = request.permission_mode
        pipeline = (
            pipeline_definition or AgentPipelineDefinition.compile()
        ).bind_runtime(
            context=context,
            observers=[TraceObserver(trace), *list(observers or [])],
        )
        try:
            tool_specs = tool_set.provider_specs()
            tool_definition_tokens = estimate_text_tokens(
                json.dumps(tool_specs, ensure_ascii=False, separators=(",", ":"))
            )
            context_plan = build_context_plan(
                [message for message in request.messages if message.carries_context()],
                prompt=request.prompt,
                model_config=request.model_config,
                context_summary=request.context_summary,
                tool_definition_tokens=tool_definition_tokens,
            )
            # compose 后的 messages 含 system 和 provider 协议消息，是唯一驱动
            # ReAct 的列表；会话持久化另由流式事件完成。
            messages = compose_messages(
                context_plan.messages,
                prompt=request.prompt,
                context_summary=context_plan.summary,
                attachments=request.attachments,
                working_set_context=request.working_set_context,
            )
            selected_model = request.model_config.get(
                "model", runtime_config.openai_model
            )
            client = AsyncOpenAI(
                api_key=(
                    request.model_config.get("apiKey") or runtime_config.openai_api_key
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
            "tool_set": tool_set,
            "dispatcher": dispatcher,
            "approval_handler": approval_handler,
            "approved_targets": set(),
            "pipeline": pipeline,
            "context": context,
            "trace": trace,
            "thinking": thinking,
            "tool_specs": tool_specs,
            "context_plan": context_plan,
            "messages": messages,
            "selected_model": selected_model,
            "tool_definition_tokens": tool_definition_tokens,
            "tool_policies": dict(tool_set.context_policies()),
            "client": client,
            "loaded_memory_paths": set(
                [
                    *(
                        request.prompt.initial_memory_paths
                        if request.prompt is not None
                        else ()
                    ),
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
        - **Act**：按模型给出的顺序逐个执行本地/MCP 工具。权限、运行期白名单、
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

        request: AgentRunRequest = runtime[
            "request"
        ]  # 本轮已拼好的 prompt / 消息 / 权限入参
        config: Settings = runtime["config"]  # 迭代上限、默认模型、状态文案
        trace: list[str] = runtime["trace"]  # 内部轨迹，最终打进 final.payload
        thinking: list[dict[str, Any]] = runtime[
            "thinking"
        ]  # UI 思考步骤，供 thinking 事件
        selected_model: str = runtime["selected_model"]  # 实际发给 provider 的模型 id
        client: AsyncOpenAI = runtime["client"]  # 本轮专用客户端（apiKey / baseUrl）
        context: AgentRunContext = runtime[
            "context"
        ]  # 本轮 run 元数据（含 permission_mode）
        pipeline: AgentPipelineRuntime = runtime[
            "pipeline"
        ]  # Observer / Middleware 管线
        tool_set = runtime["tool_set"]
        tool_specs: list[dict[str, Any]] = runtime[
            "tool_specs"
        ]  # chat.completions 的 tools schema
        context_plan = runtime["context_plan"]  # 裁剪 / 摘要后的上下文预算
        # 含 system 的 provider 协议列表，也是 ReAct 循环唯一的消息状态。
        messages: list[dict[str, Any]] = runtime["messages"]
        loaded_memory_paths: set[str] = runtime["loaded_memory_paths"]
        context_state = dict(request.context_state or {})
        tool_definition_tokens = int(runtime.get("tool_definition_tokens", 0))
        tool_policies = dict(runtime.get("tool_policies") or {})
        pending_replacements: dict[str, dict[str, Any]] = {}
        reactive_attempted = False
        auto_compact_attempted = False
        latest_input_usage: int | None = None
        usage_baseline_estimate: int | None = None
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
                    compacted_message_count=0,
                    summary_chars=len(context_plan.summary),
                    attachment_count=len(request.attachments),
                    auto_compacted=False,
                    budget=context_plan.budget.as_dict(),
                    breakdown=dict(context_plan.breakdown),
                ),
            )
            # -----------------------------------------------------------------
            # 开场：还不是 ReAct 一步。把本轮上下文预算和 memory/MCP 痕迹推给
            # 前端，再进入循环。不要发合成 thinking：那不是模型推理。
            # trace[-1] 来自 AgentStarted。
            # -----------------------------------------------------------------
            yield {
                "type": "context_state",
                "payload": {
                    **context_plan.as_dict(),
                    "loadedMemoryPaths": sorted(loaded_memory_paths),
                },
            }
            if loaded_memory_paths:
                trace.append(f"memory:eager_loaded:{len(loaded_memory_paths)} files")
            trace.append(f"tools:catalog:{len(tool_set.bindings)} tools")

            # -----------------------------------------------------------------
            # 可选：从 HITL 中断点续跑 Act（不重放 Reason）
            #
            # checkpoint 停在某一轮 Reason 之后、某一批 tool_calls 之中。
            # 当时模型输出已经流式给过前端，所以这里只补执行剩余工具，把
            # Observation 写回 messages，然后从 iteration+1 再 Reason。
            # -----------------------------------------------------------------
            start_iteration = 0
            continuation_checkpoint = runtime.get("continuation_checkpoint")
            if isinstance(continuation_checkpoint, dict):
                if continuation_checkpoint.get("kind") != "context_continuation":
                    raise RuntimeError("K Agent continuation checkpoint is invalid")
                if continuation_checkpoint.get(
                    "contextGeneration"
                ) != context_state.get("generation"):
                    raise RuntimeError("K Agent continuation generation is stale")
                runtime["dispatcher"].restore_state(runtime, continuation_checkpoint)
                checkpoint_messages = continuation_checkpoint.get("modelMessages")
                if not isinstance(checkpoint_messages, list):
                    raise RuntimeError("K Agent continuation messages are missing")
                # system/request context 使用本次重新编译的最新内容；只用 checkpoint
                # 恢复 boundary 后同一执行段的会话消息，绝不复用旧 system prompt。
                prefix = [
                    dict(message)
                    for message in messages
                    if message.get("role") == "system"
                    or message.get("_request_context") is True
                ]
                messages = [
                    *prefix,
                    *[dict(message) for message in checkpoint_messages],
                ]
                start_iteration = int(continuation_checkpoint.get("iteration", 0))
                loaded_memory_paths.update(
                    str(item)
                    for item in continuation_checkpoint.get("loadedMemoryPaths", [])
                    if isinstance(item, str)
                )
                reactive_attempted = bool(
                    continuation_checkpoint.get("reactiveAttempted", False)
                )
            if isinstance(resume_checkpoint, dict):
                if resume_checkpoint.get("kind") != "react_tool_boundary":
                    raise RuntimeError(
                        "K Agent checkpoint is not a ReAct tool boundary"
                    )
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


                runtime["dispatcher"].restore_state(runtime, resume_checkpoint)
                for index in range(pending_index, len(pending_calls)):
                    tc = pending_calls[index]
                    if not isinstance(tc, dict):
                        raise RuntimeError(
                            "K Agent checkpoint contains an invalid tool call"
                        )
                    call_id = str(tc.get("id") or "")
                    tool_name = str(tc.get("name") or "")
                    if not call_id or not tool_name:
                        raise RuntimeError(
                            "K Agent checkpoint tool identity is missing"
                        )
                    raw_arguments = str(tc.get("arguments") or "{}")
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
                    resumed_outcome = (
                        runtime["dispatcher"].resume_outcome(
                            runtime,
                            tool_name,
                            raw_arguments,
                            resume_decision,
                            decision_payload,
                        )
                        if index == pending_index
                        else None
                    )
                    if resumed_outcome is not None:
                        tool_result = resumed_outcome.public_content
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
                            "toolScope": runtime["dispatcher"].checkpoint_state(
                                runtime
                            ),
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
                        await self._model_observation(
                            runtime,
                            call_id=call_id,
                            tool_result=tool_result,
                        )
                        if tool_executed
                        else tool_result
                    )
                    observation, replacement = limit_tool_result(
                        observation,
                        tool_name=tool_name,
                        message_id=tool_message_id,
                        tool_call_id=call_id,
                        policy=tool_policies.get(tool_name, ToolContextPolicy()),
                    )
                    if replacement is not None:
                        pending_replacements[tool_message_id] = replacement
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": observation,
                            "_message_id": tool_message_id,
                        }
                    )
                # 本轮 Reason 已在旧 run 完成；从下一 iteration 重新 Reason。
                start_iteration = checkpoint_iteration + 1

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
                micro = microcompact(messages, policies=tool_policies)
                messages = micro.messages
                for replacement in micro.replacements:
                    pending_replacements[str(replacement["messageId"])] = replacement
                if micro.replacements:
                    log_event(
                        "context_microcompacted",
                        threadId=context.thread_id,
                        runId=context.run_id,
                        generation=context_state.get("generation", 0),
                        iteration=iteration,
                        replacementCount=len(micro.replacements),
                        beforeChars=micro.before_chars,
                        afterChars=micro.after_chars,
                    )
                    await pipeline.emit_context_pruned(
                        ContextPrunePayload(
                            iteration=iteration,
                            pruned_output_count=len(micro.replacements),
                            before_chars=micro.before_chars,
                            after_chars=micro.after_chars,
                        ),
                    )

                # 每次 Reason 都重新计量，包含本轮刚产生的工具结果。旧实现只在
                # create_runtime 检查一次，无法阻止一个长工具链在同 turn 撑爆窗口。
                decision = calculate_context_budget(
                    model_config=request.model_config,
                    messages=messages,
                    tool_definition_tokens=tool_definition_tokens,
                    latest_input_usage=latest_input_usage,
                    usage_baseline_estimate=usage_baseline_estimate,
                )
                log_event(
                    "context_budget_checked",
                    threadId=context.thread_id,
                    runId=context.run_id,
                    iteration=iteration,
                    generation=context_state.get("generation", 0),
                    estimatedInput=decision.budget.estimated_input,
                    warning=decision.warning,
                    needsCompact=decision.needs_compact,
                    hardLimit=decision.hard_limit,
                )
                yield {
                    "type": "context_budget",
                    "payload": {
                        **decision.budget.as_dict(),
                        "warning": decision.warning,
                        "iteration": iteration,
                    },
                }
                if decision.warning:
                    yield {
                        "type": "context_warning",
                        "payload": {
                            "estimatedInput": decision.budget.estimated_input,
                            "warningThreshold": decision.budget.warning_threshold,
                            "autoCompactThreshold": decision.budget.auto_compact_threshold,
                        },
                    }
                failure_state = context_state.get("failureState")
                auto_disabled = bool(
                    failure_state.get("autoDisabled")
                    if isinstance(failure_state, dict)
                    else False
                )
                auto_enabled = (
                    request.model_config.get("autoCompactEnabled", True) is not False
                )
                if (
                    decision.needs_compact
                    and auto_enabled
                    and not auto_disabled
                    and not auto_compact_attempted
                ):
                    auto_compact_attempted = True
                    compact_model = dict(
                        runtime.get("compact_model") or request.model_config
                    )
                    compact_client = AsyncOpenAI(
                        api_key=compact_model.get("apiKey") or config.openai_api_key,
                        base_url=compact_model.get("baseUrl") or config.openai_base_url,
                    )
                    log_event(
                        "context_compact_started",
                        threadId=context.thread_id,
                        runId=context.run_id,
                        generation=context_state.get("generation", 0),
                        model=compact_model.get("id") or compact_model.get("model"),
                        trigger="auto",
                    )
                    try:
                        compact_result = await generate_compaction(
                            client=compact_client,
                            model_config=compact_model,
                            target_model_config=request.model_config,
                            messages=messages,
                            context_state=context_state,
                            source_run_id=context.run_id,
                            trigger="auto",
                            continuation=True,
                            iteration=iteration,
                            loaded_memory_paths=sorted(loaded_memory_paths),
                            approved_targets=sorted(
                                runtime.get("approved_targets") or []
                            ),
                            working_set=runtime.get("working_set"),
                        )
                    except CompactError as exc:
                        log_event(
                            "context_compact_failed",
                            threadId=context.thread_id,
                            runId=context.run_id,
                            generation=context_state.get("generation", 0),
                            trigger="auto",
                            errorCode=exc.code,
                        )
                        yield {
                            "type": "context_compact_failed",
                            "payload": {
                                "code": exc.code,
                                "automatic": True,
                                "hardLimit": decision.hard_limit,
                            },
                        }
                        if decision.hard_limit:
                            raise RuntimeError(
                                "Context compaction failed at the hard request limit; "
                                "the full conversation history is still preserved. "
                                "Retry /compact or reduce the current attachment."
                            ) from exc
                    else:
                        log_event(
                            "context_compact_succeeded",
                            threadId=context.thread_id,
                            runId=context.run_id,
                            generation=context_state.get("generation", 0),
                            **dict(compact_result.proposal.get("stats") or {}),
                        )
                        proposal = dict(compact_result.proposal)
                        proposal["toolReplacements"] = _merge_tool_replacements(
                            context_state.get("toolReplacements"),
                            pending_replacements.values(),
                        )
                        checkpoint = dict(compact_result.continuation_checkpoint or {})
                        checkpoint["toolScope"] = runtime[
                            "dispatcher"
                        ].checkpoint_state(runtime)
                        checkpoint["compactCount"] = (
                            int((continuation_checkpoint or {}).get("compactCount", 0))
                            + 1
                        )
                        if checkpoint["compactCount"] > 2:
                            raise RuntimeError(
                                "A public run cannot perform more than two full compactions"
                            )
                        yield {
                            "type": "context_compaction_required",
                            "payload": {
                                # 已含 LLM 摘要（proposal.summary.text），但未写盘。
                                # Access Layer CAS 后才生效；coveredThroughSeq / digest 由本机 history 计算。
                                "proposal": proposal,
                                "continuationCheckpoint": checkpoint,
                            },
                        }
                        return
                elif decision.hard_limit:
                    if not reactive_attempted and not isinstance(
                        continuation_checkpoint, dict
                    ):
                        # 统一交给外层精确的 context-length 分支做一次 reactive
                        # compact；continuation 的首轮仍超 hard 时禁止再循环逃生。
                        raise RuntimeError(
                            "context length hard request threshold reached"
                        )
                    reason = (
                        "automatic compaction is disabled"
                        if auto_disabled
                        else "automatic compaction is not enabled"
                    )
                    raise RuntimeError(
                        f"Context reached the hard request limit and {reason}; "
                        "the full history is preserved. Run /compact manually."
                    )

                # ----- Reason -----
                model_request = ModelCallPayload(
                    iteration=iteration,
                    model=selected_model,
                    messages=tuple(dict(message) for message in messages),
                    tools=tuple(tool_specs),
                    reasoning_effort=request.reasoning_effort,
                )
                usage_baseline_estimate = (
                    estimate_message_tokens(list(model_request.messages))
                    + tool_definition_tokens
                )
                # provider reasoning 直接映射为 start/delta/end，不再造
                # thinking 快照、写死标题或在 Agent 内累加展示文本。
                # 一次 model call 只有一条 reasoning message，内外层事件共用同一 ID。
                reasoning_id: str | None = None
                # 可见正文的 messageId；第一个 token 就要 message_start，必须先有 id。
                message_id = str(uuid.uuid4())
                # 还没出过可见文字。只思考/只调工具时不要发空的 message_start。
                message_started = False
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
                            yield {
                                "type": "message_start",
                                "payload": {"messageId": message_id},
                            }
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
                latest_input_usage = model_result.input_tokens

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
                    result = self._final_state(
                        model_result.output_text, trace, thinking
                    )
                    agent_scope.complete(result)
                    await agent_scope.__aexit__(None, None, None)
                    if pending_replacements:
                        yield {
                            "type": "context_patch",
                            "payload": {
                                "proposalId": str(uuid.uuid4()),
                                "expectedRevision": int(
                                    context_state.get("revision", 0)
                                ),
                                "toolReplacements": list(pending_replacements.values()),
                            },
                        }
                    yield {"type": "final", "payload": {"output": result["output"]}}
                    return

                # ----- Act：按顺序执行本轮全部 tool_calls（不并行）-----
                # 并行会打乱 Observation 顺序，也让 HITL checkpoint 的 pendingIndex
                # 失去「做到第几个」的含义。同轮多个工具仍是一次 Reason 的产物。
                #
                # 带 tool_calls 的 assistant 必须进 messages，后续 tool 消息才能配对。
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": model_result.output_text or None,
                    "_message_id": message_id,
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
                    raw_arguments = tc["arguments"]
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
                        "toolScope": runtime["dispatcher"].checkpoint_state(runtime),
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
                    notice_handler = runtime.get("tool_notice")
                    notice = (
                        notice_handler(context, tool_name, tool_result, config)
                        if notice_handler
                        else None
                    )
                    if notice is not None:
                        logger.info("Sandbox notice: %s", notice)
                    observation = (
                        await self._model_observation(
                            runtime,
                            call_id=tc["id"],
                            tool_result=tool_result,
                        )
                        if tool_executed
                        else tool_result
                    )
                    observation, replacement = limit_tool_result(
                        observation,
                        tool_name=tool_name,
                        message_id=tool_message_id,
                        tool_call_id=tc["id"],
                        policy=tool_policies.get(tool_name, ToolContextPolicy()),
                    )
                    if replacement is not None:
                        pending_replacements[tool_message_id] = replacement
                        log_event(
                            "context_tool_result_limited",
                            threadId=context.thread_id,
                            runId=context.run_id,
                            generation=context_state.get("generation", 0),
                            iteration=iteration,
                            tool=tool_name,
                            originalChars=replacement.get("originalChars"),
                            retainedChars=len(observation),
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": observation,
                            "_message_id": tool_message_id,
                        }
                    )
                # 本轮 Act 全部 Observe 完毕；status 切回「正在调用模型」，进入下一 Reason。

            # ----- Finish：迭代上限（兜底，不是模型主动结束）-----
            # 最后一轮 Reason 仍请求了工具，但不再 Act，以免无限转。上限文案
            # 既流式发给用户，也作为明确 output 交给完成 Observer。
            limit_message = config.tool_iteration_limit_message
            assistant_message_id = str(uuid.uuid4())
            yield {
                "type": "message_start",
                "payload": {"messageId": assistant_message_id},
            }
            yield {
                "type": "delta",
                "payload": {
                    "messageId": assistant_message_id,
                    "content": limit_message,
                },
            }
            yield {
                "type": "message_end",
                "payload": {"messageId": assistant_message_id},
            }
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
            if pending_replacements:
                yield {
                    "type": "context_patch",
                    "payload": {
                        "proposalId": str(uuid.uuid4()),
                        "expectedRevision": int(context_state.get("revision", 0)),
                        "toolReplacements": list(pending_replacements.values()),
                    },
                }
            yield {"type": "final", "payload": {"output": result["output"]}}
        except (asyncio.CancelledError, GeneratorExit) as exc:
            # 取消必须关掉模型/Agent 观测，但绝不能变成模型可见的 tool 错误，
            # 也不能伪装成 AG-UI 成功 Finish。
            await agent_scope.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        except Exception as exc:
            if is_context_length_error(exc) and not reactive_attempted:
                compact_model = dict(
                    runtime.get("compact_model") or request.model_config
                )
                compact_client = AsyncOpenAI(
                    api_key=compact_model.get("apiKey") or config.openai_api_key,
                    base_url=compact_model.get("baseUrl") or config.openai_base_url,
                )
                log_event(
                    "context_compact_started",
                    threadId=context.thread_id,
                    runId=context.run_id,
                    generation=context_state.get("generation", 0),
                    model=compact_model.get("id") or compact_model.get("model"),
                    trigger="reactive",
                )
                try:
                    compact_result = await generate_compaction(
                        client=compact_client,
                        model_config=compact_model,
                        target_model_config=request.model_config,
                        messages=messages,
                        context_state=context_state,
                        source_run_id=context.run_id,
                        trigger="reactive",
                        continuation=True,
                        iteration=locals().get("iteration", 0),
                        loaded_memory_paths=sorted(loaded_memory_paths),
                        approved_targets=sorted(runtime.get("approved_targets") or []),
                        working_set=runtime.get("working_set"),
                    )
                except CompactError as compact_exc:
                    log_event(
                        "context_compact_failed",
                        threadId=context.thread_id,
                        runId=context.run_id,
                        generation=context_state.get("generation", 0),
                        trigger="reactive",
                        errorCode=compact_exc.code,
                    )
                    yield {
                        "type": "context_compact_failed",
                        "payload": {
                            "code": compact_exc.code,
                            "automatic": True,
                            "hardLimit": True,
                        },
                    }
                    await agent_scope.__aexit__(
                        type(compact_exc), compact_exc, compact_exc.__traceback__
                    )
                    raise RuntimeError(
                        "Reactive context compaction failed; full conversation history is preserved."
                    ) from compact_exc
                proposal = dict(compact_result.proposal)
                log_event(
                    "context_compact_succeeded",
                    threadId=context.thread_id,
                    runId=context.run_id,
                    generation=context_state.get("generation", 0),
                    **dict(proposal.get("stats") or {}),
                )
                proposal["toolReplacements"] = _merge_tool_replacements(
                    context_state.get("toolReplacements"),
                    pending_replacements.values(),
                )
                checkpoint = dict(compact_result.continuation_checkpoint or {})
                checkpoint["toolScope"] = runtime["dispatcher"].checkpoint_state(
                    runtime
                )
                checkpoint["reactiveAttempted"] = True
                checkpoint["compactCount"] = (
                    int((continuation_checkpoint or {}).get("compactCount", 0)) + 1
                )
                if checkpoint["compactCount"] > 2:
                    raise RuntimeError(
                        "A public run cannot perform more than two full compactions"
                    )
                await agent_scope.__aexit__(None, None, None)
                yield {
                    "type": "context_compaction_required",
                    "payload": {
                        "proposal": proposal,
                        "continuationCheckpoint": checkpoint,
                    },
                }
                return
            # Agent 级异常（模型不可达、上下文构建失败等）不像工具失败那样可恢复。
            # 记入观测后继续上抛，由 agui 转成 RUN_ERROR。
            await agent_scope.__aexit__(type(exc), exc, exc.__traceback__)
            # 循环前失败时 trace 可能仍为空，不能盲取 trace[-1] 盖掉真正错误。
            logger.exception(
                "Agent run failed at %s", trace[-1] if trace else type(exc).__name__
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
            # 上层取消 run 时，输出等待和执行任务都要停，避免工具在后台继续跑。
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
            "messages": sanitize_provider_messages(
                [dict(message) for message in request.messages]
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_output_tokens,
            "timeout": config.model_request_timeout_seconds,
        }
        if request.tools:
            kwargs["tools"] = [dict(tool) for tool in request.tools]
            kwargs["tool_choice"] = "auto"
        if request.reasoning_effort and request.reasoning_effort != "none":
            kwargs["reasoning_effort"] = request.reasoning_effort

        started_at = time.perf_counter()
        try:
            stream = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            # 部分 OpenAI-compatible Provider 尚未实现 stream_options。usage 是
            # 优选计量信号而非可用性硬依赖，只对明确的不支持错误退回估算器。
            if (
                "stream_options" not in str(exc).lower()
                and "include_usage" not in str(exc).lower()
            ):
                raise
            kwargs.pop("stream_options", None)
            stream = await client.chat.completions.create(**kwargs)
        content_buffer = ""
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        response_id = ""
        input_tokens: int | None = None
        async for chunk in self._iter_stream_with_idle_timeout(stream, config):
            if chunk.id:
                response_id = chunk.id
            usage = getattr(chunk, "usage", None)
            prompt_tokens = (
                getattr(usage, "prompt_tokens", None) if usage is not None else None
            )
            if isinstance(prompt_tokens, int):
                input_tokens = prompt_tokens
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
                input_tokens=input_tokens,
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

    async def _run_tool(self, runtime, iteration, call_id, tool_name, arguments) -> str:
        """Agent 只负责调用配对；解析、安全门和模型正文由 Dispatcher 持有。"""
        outcome = await runtime["dispatcher"].dispatch(
            runtime,
            ProviderToolCall(
                call_id,
                iteration,
                tool_name,
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False),
            ),
        )
        return outcome.public_content

    async def _model_observation(self, runtime, *, call_id, tool_result) -> str:
        outcome = runtime.get("_tool_outcomes", {}).pop(call_id, None)
        return outcome.model_content if outcome is not None else tool_result

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
            if message.get("role") == "tool" and isinstance(message.get("content"), str)
        ]

    @classmethod
    def _tool_output_chars(cls, messages: list[dict[str, Any]]) -> int:
        """Count tool-output characters without exposing their content to logs."""

        return sum(len(content) for content in cls._tool_output_contents(messages))
