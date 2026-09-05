"""Access Layer 落盘形态：流式 delta 只存在于 SSE，会话记录只保存累计后的块。

SSE 仍按 AG-UI 逐 token 推给前端；`SessionStore.append_event` 在结构边界把
同一 `messageId` / `toolCallId` 的增量合成一条 `delta`（此时已是全文）。
打开历史时按整块赋值，不再把多条 token 拼回去。
"""

from __future__ import annotations

from typing import Any


INCREMENTAL_EVENT_TYPES = frozenset({
    "TEXT_MESSAGE_CONTENT",
    "TOOL_CALL_ARGS",
    "REASONING_MESSAGE_CONTENT",
})


def incremental_event_key(event: dict[str, Any]) -> tuple[Any, ...] | None:
    """同一增量流的合并键；非增量事件返回 None。"""

    event_type = str(event.get("type") or "")
    if event_type == "TEXT_MESSAGE_CONTENT" or event_type == "REASONING_MESSAGE_CONTENT":
        return (event_type, event.get("messageId"))
    if event_type == "TOOL_CALL_ARGS":
        return (event_type, event.get("toolCallId"))
    return None


def coalesce_durable_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把历史上逐 token 落下的 CONTENT/ARGS 合成每块一条。

    相邻、同键的增量合并为一条，`delta` 为全文。工具 stdout 的
    `tool_output_delta` 只服务实时流，落盘记录里丢弃。
    """

    coalesced: list[dict[str, Any]] = []
    pending_key: tuple[Any, ...] | None = None
    pending_event: dict[str, Any] | None = None
    pending_parts: list[str] = []

    def flush() -> None:
        nonlocal pending_key, pending_event, pending_parts
        if pending_event is None:
            return
        merged = dict(pending_event)
        merged["delta"] = "".join(pending_parts)
        coalesced.append(merged)
        pending_key = None
        pending_event = None
        pending_parts = []

    for event in events:
        if not isinstance(event, dict):
            flush()
            coalesced.append(event)
            continue
        if event.get("type") == "CUSTOM" and event.get("name") == "tool_output_delta":
            continue
        key = incremental_event_key(event)
        if key is None:
            flush()
            coalesced.append(event)
            continue
        delta = event.get("delta")
        chunk = delta if isinstance(delta, str) else ""
        if pending_key == key and pending_event is not None:
            pending_parts.append(chunk)
            continue
        flush()
        pending_key = key
        pending_event = event
        pending_parts = [chunk]
    flush()
    return coalesced
