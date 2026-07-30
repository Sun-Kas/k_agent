"""Estimate prompt usage and compact older conversation context within model limits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any

from backend.api.schemas import ChatMessage


DEFAULT_CONTEXT_WINDOW = 1000_000
DEFAULT_MAX_OUTPUT_TOKENS = 50_000
DEFAULT_SAFETY_TOKENS = 200_000
MIN_RECENT_MESSAGES = 6
MAX_SUMMARY_CHARS = 100_000
MAX_SUMMARY_MESSAGE_CHARS = 10_000
MAX_TOOL_CONTEXT_CHARS = 50_000


@dataclass(frozen=True)
class ContextBudget:
    """Token allocation reserved for input after output and safety margins."""

    context_window: int
    max_output_tokens: int
    safety_tokens: int
    input_budget: int

    def as_dict(self) -> dict[str, int]:
        """把对象转换为可序列化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class ContextPlan:
    """Messages and summary selected for a run, with an auditable budget breakdown."""

    messages: list[ChatMessage]
    summary: str
    compacted_message_ids: list[str]
    budget: ContextBudget
    breakdown: dict[str, int]
    auto_compacted: bool

    def as_dict(self) -> dict[str, Any]:
        """把对象转换为可序列化字典。"""
        return {
            "summary": self.summary,
            "compactedMessageIds": self.compacted_message_ids,
            "budget": self.budget.as_dict(),
            "breakdown": self.breakdown,
            "autoCompacted": self.auto_compacted,
            "messageCount": len(self.messages),
        }


def build_context_plan(
    messages: list[ChatMessage],
    *,
    system_prompt: str,
    user_context: dict[str, str],
    model_config: dict[str, Any],
    existing_summary: str = "",
    compacted_message_ids: list[str] | None = None,
    tool_definition_tokens: int = 0,
    force_compact: bool = False,
) -> ContextPlan:
    """Build a context window plan and compact older messages when required."""

    budget = _context_budget(model_config)
    compacted = set(compacted_message_ids or [])
    active = [message for message in messages if message.id not in compacted]
    system_tokens = estimate_text_tokens(system_prompt)
    memory_tokens = estimate_text_tokens("\n".join(user_context.values()))
    summary_tokens = estimate_text_tokens(existing_summary)
    message_tokens = estimate_message_tokens(active)
    fixed_tokens = system_tokens + memory_tokens + summary_tokens + tool_definition_tokens
    available_for_messages = max(1_000, budget.input_budget - fixed_tokens)
    should_compact = force_compact or message_tokens > available_for_messages

    summary = existing_summary
    newly_compacted: list[str] = []
    if should_compact and len(active) > 2:
        summary, newly_compacted, active = compact_messages(
            active,
            existing_summary=existing_summary,
            message_token_budget=available_for_messages,
            force=force_compact,
        )
        compacted.update(newly_compacted)
        summary_tokens = estimate_text_tokens(summary)
        message_tokens = estimate_message_tokens(active)

    breakdown = {
        "system": system_tokens,
        "memory": memory_tokens,
        "skillsAndTools": max(0, tool_definition_tokens),
        "summary": summary_tokens,
        "messages": message_tokens,
        "estimatedInput": system_tokens + memory_tokens + tool_definition_tokens + summary_tokens + message_tokens,
        "inputBudget": budget.input_budget,
        "remaining": max(
            0,
            budget.input_budget
            - system_tokens
            - memory_tokens
            - tool_definition_tokens
            - summary_tokens
            - message_tokens,
        ),
    }
    return ContextPlan(
        messages=active,
        summary=summary,
        compacted_message_ids=sorted(compacted),
        budget=budget,
        breakdown=breakdown,
        auto_compacted=bool(newly_compacted),
    )


def compact_messages(
    messages: list[ChatMessage],
    *,
    existing_summary: str = "",
    message_token_budget: int = 24_000,
    force: bool = False,
) -> tuple[str, list[str], list[ChatMessage]]:
    """Summarize older messages while preserving a minimum recent-message tail."""

    if len(messages) <= 2:
        return existing_summary, [], messages
    split = max(1, len(messages) - MIN_RECENT_MESSAGES)
    if not force and estimate_message_tokens(messages) <= message_token_budget:
        return existing_summary, [], messages
    if len(messages) <= MIN_RECENT_MESSAGES:
        split = max(1, len(messages) - 2)
    if not force:
        recent = messages[split:]
        while split > 0 and estimate_message_tokens(recent) < message_token_budget * 0.7:
            candidate = messages[split - 1 :]
            if estimate_message_tokens(candidate) > message_token_budget * 0.78:
                break
            split -= 1
            recent = candidate
    older = messages[:split]
    if not older:
        return existing_summary, [], messages
    summary = _merge_summary(existing_summary, older)
    return summary, [message.id for message in older], messages[split:]


def prune_old_tool_outputs(
    messages: list[dict[str, Any]],
    *,
    max_tool_chars: int = MAX_TOOL_CONTEXT_CHARS,
    keep_recent: int = 2,
) -> list[dict[str, Any]]:
    """Replace oldest large tool outputs while retaining recent exact results."""

    tool_indexes = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "tool" and isinstance(message.get("content"), str)
    ]
    total = sum(len(str(messages[index].get("content") or "")) for index in tool_indexes)
    if total <= max_tool_chars:
        return messages
    protected = set(tool_indexes[-keep_recent:])
    pruned = [dict(message) for message in messages]
    for index in tool_indexes:
        if total <= max_tool_chars or index in protected:
            continue
        content = str(pruned[index].get("content") or "")
        total -= len(content)
        pruned[index]["content"] = (
            f"[Older tool output cleared from active context: {len(content)} characters. "
            "Run the tool again if the exact output is needed.]"
        )
        total += len(pruned[index]["content"])
    return pruned


def compose_api_messages(
    messages: list[ChatMessage],
    *,
    system_prompt: str,
    user_context: dict[str, str],
    attachments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """组装 provider 请求需要的 system/user/context/messages/attachments。"""
    body: list[dict[str, Any]] = []
    attachments = attachments or []
    for index, message in enumerate(messages):
        content: Any = message.content
        if attachments and index == len(messages) - 1 and message.role == "user":
            content = [{"type": "text", "text": message.content}]
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": attachment["dataUrl"]},
                }
                for attachment in attachments
                if attachment.get("dataUrl")
            )
        body.append({"role": message.role, "content": content})
    reminder = _render_user_context(user_context)
    return [
        {"role": "system", "content": system_prompt},
        *([{"role": "user", "content": reminder}] if reminder else []),
        *body,
    ]


def estimate_text_tokens(value: str) -> int:
    """Estimate mixed ASCII/CJK token usage without a model-specific tokenizer."""

    if not value:
        return 0
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    non_ascii_chars = len(value) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars / 1.6))


def estimate_message_tokens(messages: list[ChatMessage] | list[dict[str, Any]]) -> int:
    """估算消息列表占用的 token 数。"""
    total = 0
    for message in messages:
        if isinstance(message, dict):
            role = str(message.get("role") or "")
            content = message.get("content")
        else:
            role = message.role
            content = message.content
        rendered = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        total += 4 + estimate_text_tokens(role) + estimate_text_tokens(rendered)
    return total


def _context_budget(model_config: dict[str, Any]) -> ContextBudget:
    """从模型配置计算可用输入 token 预算。"""
    context_window = _positive_int(model_config.get("contextWindow"), DEFAULT_CONTEXT_WINDOW)
    max_output = _positive_int(model_config.get("maxOutputTokens"), DEFAULT_MAX_OUTPUT_TOKENS)
    safety = _positive_int(model_config.get("contextSafetyTokens"), DEFAULT_SAFETY_TOKENS)
    input_budget = max(8_000, context_window - max_output - safety)
    return ContextBudget(context_window, max_output, safety, input_budget)


def _positive_int(value: object, default: int) -> int:
    """把配置值安全解析为正整数。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _merge_summary(existing: str, messages: list[ChatMessage]) -> str:
    """把旧消息合并进上下文摘要。"""
    sections = [existing.strip()] if existing.strip() else []
    sections.append("# Compacted conversation")
    for message in messages:
        content = " ".join(message.content.split())
        if len(content) > MAX_SUMMARY_MESSAGE_CHARS:
            content = content[:MAX_SUMMARY_MESSAGE_CHARS].rstrip() + "…"
        label = {
            "user": "User request",
            "assistant": "Assistant result",
            "tool": f"Tool result ({message.meta.tool_name if message.meta else 'tool'})",
            "system": "System note",
        }.get(message.role, message.role)
        sections.append(f"- {label}: {content}")
    merged = "\n".join(section for section in sections if section)
    if len(merged) > MAX_SUMMARY_CHARS:
        merged = merged[-MAX_SUMMARY_CHARS:]
        merged = "# Earlier compacted context omitted\n" + merged
    return merged


def _render_user_context(context: dict[str, str]) -> str:
    """把 memory 和动态上下文渲染成 system-reminder。"""
    body = "\n".join(f"# {key}\n{value}" for key, value in context.items() if value)
    if not body:
        return ""
    return (
        "<system-reminder>\n"
        "As you answer the user's questions, you can use the following context:\n"
        f"{body}\n\n"
        "IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context "
        "unless it is highly relevant to the user's request.\n"
        "</system-reminder>"
    )
