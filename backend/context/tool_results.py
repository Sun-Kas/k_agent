"""单条工具结果预算与保留 tool-call 配对的 microcompact。"""

from __future__ import annotations

import hashlib
from typing import Any

from backend.context.models import MicrocompactResult, ToolResultPolicy


_RERUNNABLE = frozenset({"Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch"})
_RECEIPT = frozenset({"Write", "Edit", "NotebookEdit", "TodoWrite"})


def policy_for_tool(
    tool_name: str,
    declared: dict[str, Any] | None = None,
    *,
    source: str = "local",
) -> ToolResultPolicy:
    """MCP/未知工具默认 retain；本地已知工具采用显式安全策略。"""

    if isinstance(declared, dict):
        mode = str(declared.get("mode") or "retain")
        maximum = declared.get("maxResultChars")
        if mode in {"rerunnable", "receipt", "retain"}:
            return ToolResultPolicy(mode=mode, max_result_chars=_max_chars(maximum))
    if source != "local":
        return ToolResultPolicy()
    if tool_name in _RERUNNABLE:
        return ToolResultPolicy("rerunnable", 30_000)
    if tool_name in _RECEIPT:
        return ToolResultPolicy("receipt", 12_000)
    return ToolResultPolicy()


def limit_tool_result(
    content: str,
    *,
    tool_name: str,
    message_id: str,
    tool_call_id: str,
    policy: ToolResultPolicy,
) -> tuple[str, dict[str, Any] | None]:
    """限制单条 Observation；公开 AG-UI 仍保存调用方传入的完整 content。"""

    if policy.mode == "retain" or len(content) <= policy.max_result_chars:
        return content, None
    replacement = _replacement(content, tool_name=tool_name, policy=policy)
    return replacement, {
        "messageId": message_id,
        "toolCallId": tool_call_id,
        "sourceDigest": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "replacement": replacement,
        "originalChars": len(content),
        "reason": "result_budget",
    }


def microcompact(
    messages: list[dict[str, Any]],
    *,
    policies: dict[str, ToolResultPolicy],
    keep_recent_rounds: int = 2,
) -> MicrocompactResult:
    """只替换旧的可重取/回执 tool 正文，永不删除 tool 消息。"""

    copied = [dict(message) for message in messages]
    call_names: dict[str, str] = {}
    tool_indexes: list[int] = []
    tool_rounds: list[list[int]] = []
    active_round: list[int] | None = None
    for index, message in enumerate(copied):
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict) and isinstance(call.get("id"), str):
                call_names[call["id"]] = str(function.get("name") or "tool")
        if message.get("role") == "assistant" and message.get("tool_calls"):
            active_round = []
            tool_rounds.append(active_round)
        if message.get("role") == "tool":
            tool_indexes.append(index)
            if active_round is not None:
                active_round.append(index)
        elif message.get("role") != "assistant":
            active_round = None
    # “最近两轮”按一次 assistant tool_calls 及其全部结果计，不按 tool
    # 消息条数计；否则一轮三个并行调用会错误挤掉上一轮的保护名额。
    protected = {
        index
        for round_indexes in tool_rounds[-max(0, keep_recent_rounds):]
        for index in round_indexes
    }
    replacements: list[dict[str, Any]] = []
    before_chars = sum(len(str(copied[index].get("content") or "")) for index in tool_indexes)
    for index in tool_indexes:
        if index in protected:
            continue
        message = copied[index]
        call_id = str(message.get("tool_call_id") or "")
        tool_name = call_names.get(call_id, "tool")
        policy = policies.get(tool_name, ToolResultPolicy())
        content = str(message.get("content") or "")
        if policy.mode == "retain" or content.startswith("[Older "):
            continue
        replacement = _replacement(content, tool_name=tool_name, policy=policy)
        if len(replacement) >= len(content):
            continue
        message["content"] = replacement
        replacements.append({
            "messageId": str(message.get("_message_id") or f"toolresult-{call_id}"),
            "toolCallId": call_id,
            "sourceDigest": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "replacement": replacement,
            "originalChars": len(content),
            "reason": "microcompact",
        })
    after_chars = sum(len(str(copied[index].get("content") or "")) for index in tool_indexes)
    return MicrocompactResult(copied, replacements, before_chars, after_chars)


def _replacement(content: str, *, tool_name: str, policy: ToolResultPolicy) -> str:
    if policy.mode == "receipt":
        notice = (
            f"[Older {tool_name} result reduced to a durable receipt; "
            f"original length: {len(content)} characters.]"
        )
        body_budget = max(0, policy.max_result_chars - len(notice) - 1)
        return (content[:body_budget].rstrip() + "\n" + notice).strip()[:policy.max_result_chars]
    notice = (
        f"[Older {tool_name} output cleared; {len(content)} characters total. "
        "Run the tool again if exact content is needed.]"
    )
    body_budget = max(0, policy.max_result_chars - len(notice) - 2)
    head_budget = body_budget // 2
    tail_budget = body_budget - head_budget
    head = content[:head_budget].rstrip()
    tail = content[-tail_budget:].lstrip() if tail_budget else ""
    return "\n".join(item for item in (head, notice, tail) if item)[:policy.max_result_chars]


def _max_chars(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 50_000
    return parsed if parsed > 0 else 50_000
