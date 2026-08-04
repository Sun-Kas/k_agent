"""Create and encode the standard AG-UI event stream emitted by Agent Backend."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ag_ui.core import (
    CustomEvent,
    EventType,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from backend.api.schemas import ChatMessage


encoder = EventEncoder()


def encode_event(event: Any) -> str:
    """把 AG-UI event 模型编码为传输文本。"""
    return encoder.encode(event)


def to_chat_messages(messages: list[Any]) -> list[ChatMessage]:
    """把 AG-UI 输入消息转换为后端 ChatMessage 并过滤空 assistant。"""
    converted: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content", "")
            message_id = message.get("id")
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", "")
            message_id = getattr(message, "id", None)
        # 未知角色和非字符串正文一律丢弃：这里是不可信输入进入后端的入口，
        # 放行畸形消息会在拼装 provider 请求时才炸，且难以定位。
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        # 空 assistant 消息是被中断或纯工具调用的残留，喂回模型没有信息量，
        # 部分 provider 还会因为空 content 直接报错。
        if role == "assistant" and not content.strip():
            continue
        converted.append(
            ChatMessage(
                id=str(message_id) if message_id else str(uuid.uuid4()),
                role=role,
                content=content,
                createdAt=datetime.now(timezone.utc),
            )
        )
    return converted


async def translate_agent_events(
    events: AsyncIterator[dict[str, Any]],
    thread_id: str,
    run_id: str,
) -> AsyncIterator[Any]:
    """把 Agent 内部事件按流式顺序转换为标准 AG-UI events。"""
    # 内部 thinking 是「同一个 step 反复整体重发、detail 越来越长」的快照语义，
    # 而 AG-UI reasoning 是「start / 增量 delta / end」的流式语义。下面四个变量
    # 就是这两种语义之间的转换状态：
    # - reasoning_message_id：外层 REASONING 块，跨多个 step 存在
    # - active_reasoning_message_id / active_reasoning_step：当前正在增量输出的 step
    #   及其上次快照，用来算出本次要发的 delta
    # - completed_reasoning_message_ids：已经发过 END 的 step，用于丢弃迟到的重复完成态
    reasoning_message_id: str | None = None
    active_reasoning_message_id: str | None = None
    active_reasoning_step: dict[str, Any] | None = None
    completed_reasoning_message_ids: set[str] = set()

    def reasoning_events(step: dict[str, Any]) -> list[Any]:
        """把内部 thinking step 转换为 AG-UI reasoning start/content/end 事件。"""
        nonlocal reasoning_message_id, active_reasoning_message_id, active_reasoning_step
        # 工具阶段的 thinking 由 TOOL_CALL_* 事件表达，不再重复成 reasoning，
        # 否则前端时间线上同一次调用会出现两条。
        if step.get("phase") == "tool":
            return []

        # thinking 在 AG-UI 中映射为 REASONING：同一个 step 只增量追加；
        # 一旦 step 完成，就发 REASONING_MESSAGE_END，后续同 ID 的迟到完成态不再重开。
        events: list[Any] = []
        step_id = str(step.get("id") or uuid.uuid4())
        detail = str(step.get("detail") or "")
        # 已收尾的 step 又来一次完成态（例如主循环最后统一更新 status），
        # 直接丢弃，避免重开一个已经结束的 reasoning 消息。
        if (
            step.get("status") != "active"
            and step_id in completed_reasoning_message_ids
            and active_reasoning_message_id != step_id
        ):
            return []
        # 外层 REASONING 块惰性开启，保证只有真的产生思考内容时前端才展开面板。
        if reasoning_message_id is None:
            reasoning_message_id = str(uuid.uuid4())
            events.append(ReasoningStartEvent(message_id=reasoning_message_id))
        if active_reasoning_message_id == step_id and active_reasoning_step is not None:
            # 同一个 step 的后续快照：正常情况下新 detail 是旧 detail 的前缀扩展，
            # 只把新增尾巴作为 delta 发出去。
            previous_detail = str(active_reasoning_step.get("detail") or "")
            active_reasoning_step = {**step, "id": step_id}
            if detail.startswith(previous_detail):
                delta = detail[len(previous_detail):]
                if delta:
                    events.append(
                        ReasoningMessageContentEvent(
                            message_id=step_id,
                            delta=delta,
                            raw_event={"id": step_id},
                        )
                    )
            else:
                # detail 被整体改写而不是追加（例如流式思考结束后换成结论摘要）。
                # 增量无法表达这种替换，只能收尾旧消息再用同一个 ID 重开一条，
                # 前端据此清空并重绘该块。
                events.append(
                    ReasoningMessageEndEvent(
                        message_id=step_id,
                        raw_event={**active_reasoning_step, "status": "complete"}
                    )
                )
                events.append(ReasoningMessageStartEvent(
                    message_id=step_id,
                    role="reasoning",
                    raw_event=active_reasoning_step,
                ))
                if detail:
                    events.append(
                        ReasoningMessageContentEvent(
                            message_id=step_id,
                            delta=detail,
                            raw_event={"id": step_id},
                        )
                    )
            if step.get("status") != "active":
                events.append(ReasoningMessageEndEvent(
                    message_id=step_id,
                    raw_event=active_reasoning_step,
                ))
                completed_reasoning_message_ids.add(step_id)
                active_reasoning_message_id = None
                active_reasoning_step = None
            return events

        # 换到了新的 step：先把上一个未收尾的 step 关掉，保证任意时刻
        # 至多只有一条 reasoning 消息处于打开状态。
        if active_reasoning_message_id is not None:
            completed_reasoning_message_ids.add(active_reasoning_message_id)
            events.append(
                ReasoningMessageEndEvent(
                    message_id=active_reasoning_message_id,
                    raw_event={"id": active_reasoning_message_id, "status": "complete"}
                )
            )
        active_reasoning_message_id = step_id
        normalized_step = {**step, "id": step_id}
        active_reasoning_step = normalized_step
        events.append(ReasoningMessageStartEvent(
            message_id=step_id,
            role="reasoning",
            raw_event=normalized_step,
        ))
        if detail:
            events.append(
                ReasoningMessageContentEvent(
                    message_id=step_id,
                    delta=detail,
                    raw_event={"id": active_reasoning_message_id},
                )
            )
        if normalized_step.get("status") != "active":
            events.append(ReasoningMessageEndEvent(
                message_id=step_id,
                raw_event=normalized_step,
            ))
            completed_reasoning_message_ids.add(step_id)
            active_reasoning_message_id = None
            active_reasoning_step = None
        return events

    def close_reasoning_events() -> list[Any]:
        """在正文、工具或结束边界关闭当前 reasoning 块。"""
        nonlocal reasoning_message_id, active_reasoning_message_id, active_reasoning_step
        if reasoning_message_id is None:
            return []
        events: list[Any] = []
        # 正文、工具、结束事件都是 reasoning 的硬边界。前端看到 end 后
        # 就应该关闭当前 thinking 块；如果后面还有 thinking，会重新 start。
        if active_reasoning_message_id is not None:
            completed_reasoning_message_ids.add(active_reasoning_message_id)
            events.append(
                ReasoningMessageEndEvent(
                    message_id=active_reasoning_message_id,
                    raw_event={"id": active_reasoning_message_id, "status": "complete"}
                )
            )
            active_reasoning_message_id = None
            active_reasoning_step = None
        events.append(ReasoningEndEvent(message_id=reasoning_message_id))
        reasoning_message_id = None
        return events

    # RUN_STARTED 在进入内部流之前就发，且刻意放在 try 之外：即使内部流第一步
    # 就抛错，前端也已经建立好本次 run 的状态，才能正确接住随后的 RUN_ERROR。
    yield RunStartedEvent(
        type=EventType.RUN_STARTED,
        thread_id=thread_id,
        run_id=run_id,
    )

    try:
        async for event in events:
            event_type = event["type"]
            payload = event["payload"]

            if event_type == "message_start":
                for reasoning_event in close_reasoning_events():
                    yield reasoning_event
                yield TextMessageStartEvent(message_id=payload["messageId"])
            elif event_type == "delta":
                # 只有非空白增量才算正文开始。模型常常先吐几个换行或空格，
                # 拿它们去关闭 reasoning 会让思考块在真正有正文前就提前收起。
                if str(payload["content"]).strip():
                    for reasoning_event in close_reasoning_events():
                        yield reasoning_event
                yield TextMessageContentEvent(
                    message_id=payload["messageId"],
                    delta=payload["content"],
                )
            elif event_type == "message_end":
                yield TextMessageEndEvent(message_id=payload["messageId"])
            elif event_type == "tool_start":
                for reasoning_event in close_reasoning_events():
                    yield reasoning_event
                # 内部事件在调用前就已拿到完整参数，不存在参数流式增量，
                # 所以这里一次性补齐 START/ARGS/END 三件套，满足 AG-UI 的事件配对要求。
                tool_call_id = payload["toolCallId"]
                yield ToolCallStartEvent(
                    tool_call_id=tool_call_id,
                    tool_call_name=payload["toolCallName"],
                )
                yield ToolCallArgsEvent(
                    tool_call_id=tool_call_id,
                    delta=payload["arguments"],
                )
                yield ToolCallEndEvent(tool_call_id=tool_call_id)
            elif event_type == "tool_result":
                yield ToolCallResultEvent(
                    message_id=payload["messageId"],
                    tool_call_id=payload["toolCallId"],
                    content=payload["content"],
                    role="tool",
                )
            elif event_type == "status":
                yield CustomEvent(name="status", value=payload)
            elif event_type == "trace":
                yield CustomEvent(name="trace", value=payload)
            elif event_type == "cli_session":
                # Persist provider-native session ids so users can opt into resume.
                yield CustomEvent(name="cli_session", value=payload)
            elif event_type == "approval_request":
                # Approval remains a custom AG-UI event because the protocol has
                # no standard bidirectional human-approval event pair.
                yield CustomEvent(name="approval_request", value=payload)
            elif event_type == "approval_resolved":
                yield CustomEvent(name="approval_resolved", value=payload)
            elif event_type == "thinking":
                for reasoning_event in reasoning_events(payload):
                    yield reasoning_event
            elif event_type == "final":
                for reasoning_event in close_reasoning_events():
                    yield reasoning_event
                yield RunFinishedEvent(
                    thread_id=thread_id,
                    run_id=run_id,
                    result={"sessionId": thread_id},
                )
    except Exception as exc:
        # 异常在这里转成 RUN_ERROR 事件而不是向上抛：HTTP 响应头早已发出，
        # 此时抛出只会让连接无声中断，前端会一直停在 running 状态。
        # 收尾 reasoning 再发错误，前端才不会留下一个永远转圈的思考块。
        for reasoning_event in close_reasoning_events():
            yield reasoning_event
        yield RunErrorEvent(message=str(exc), code="AGENT_RUN_ERROR")
