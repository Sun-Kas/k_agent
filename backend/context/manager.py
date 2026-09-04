"""Provider 消息投影、tool 配对与保守 token 估算公共函数。

持久化摘要由 ``backend.context.compact`` 的 LLM compact-only 调用生成；本模块
不再拥有机械 bullet 摘要或按消息 ID 累积的临时压缩状态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any

from backend.api.schemas import ChatMessage
from backend.prompts.models import PromptBundle


# 这些默认值只在模型配置缺字段时兜底，真实值应由 models.config.json 提供。
DEFAULT_CONTEXT_WINDOW = 1000_000
DEFAULT_MAX_OUTPUT_TOKENS = 50_000
# 安全余量吸收 token 估算误差：本模块用启发式估算而非精确 tokenizer，
# 估少了会直接触发 provider 的 context length 报错，所以宁可多留。
DEFAULT_SAFETY_TOKENS = 200_000
# 压缩后至少保留的最近消息数。低于这个数，模型会丢失当前任务的直接上下文
# （例如刚提的问题和刚回的工具结果），压缩反而比超预算更有害。


@dataclass(frozen=True)
class ContextBudget:
    """扣掉输出与安全余量后，留给输入的 token 配额。"""

    context_window: int
    max_output_tokens: int
    safety_tokens: int
    input_budget: int

    def as_dict(self) -> dict[str, int]:
        """把对象转换为可序列化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class ContextPlan:
    """Runtime 创建时的未压缩输入快照；真正预算在每次 Reason 前计算。"""

    messages: list[ChatMessage]
    summary: str
    budget: ContextBudget
    breakdown: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        """把对象转换为可序列化字典。"""
        return {
            "summary": self.summary,
            "budget": self.budget.as_dict(),
            "breakdown": self.breakdown,
            "messageCount": len(self.messages),
        }


def build_context_plan(
    messages: list[ChatMessage],
    *,
    prompt: PromptBundle | None = None,
    system_prompt: str = "",
    user_context: dict[str, str] | None = None,
    model_config: dict[str, Any],
    context_summary: str = "",
    tool_definition_tokens: int = 0,
) -> ContextPlan:
    """构建初始投影但绝不压缩；ContextController 在每次 Reason 决策。"""

    budget = _context_budget(model_config)
    active = pair_tool_messages(messages)
    effective_system = prompt.system_prompt if prompt is not None else system_prompt
    effective_context = (
        prompt.context_message or ""
        if prompt is not None
        else "\n".join((user_context or {}).values())
    )
    system_tokens = estimate_text_tokens(effective_system)
    memory_tokens = estimate_text_tokens(effective_context)
    summary_tokens = estimate_text_tokens(context_summary)
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
        summary=context_summary,
        budget=budget,
        breakdown=breakdown,
    )


def pair_tool_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """丢掉成对断裂的 tool_call / tool 半边。

    Provider 拒绝「未宣告的 tool 结果」或「无结果的 tool_call」。压缩、裁剪、
    中断都可能拆对，故在计量/发送前先修复，而不是整请求在 provider 侧失败。
    """

    answered = {
        message.meta.tool_call_id
        for message in messages
        if message.role == "tool" and message.meta and message.meta.tool_call_id
    }
    repaired: list[ChatMessage] = []
    announced: set[str] = set()
    for message in messages:
        if message.role == "tool":
            call_id = message.meta.tool_call_id if message.meta else None
            if not call_id or call_id not in announced:
                continue
            repaired.append(message)
            continue
        if message.tool_calls:
            kept = [call for call in message.tool_calls if call.id in answered]
            if not kept:
                # An assistant turn that only issued unanswered calls carries no
                # usable content once those calls are stripped.
                if not message.content.strip():
                    continue
                repaired.append(message.model_copy(update={"tool_calls": []}))
                continue
            announced.update(call.id for call in kept)
            repaired.append(
                message
                if len(kept) == len(message.tool_calls)
                else message.model_copy(update={"tool_calls": kept})
            )
            continue
        repaired.append(message)
    return repaired


def compose_api_messages(
    messages: list[ChatMessage],
    *,
    prompt: PromptBundle | None = None,
    system_prompt: str = "",
    user_context: dict[str, str] | None = None,
    context_summary: str = "",
    attachments: list[dict[str, Any]] | None = None,
    working_set_context: str = "",
) -> list[dict[str, Any]]:
    """组装 provider 请求需要的 system/user/context/messages/attachments。"""
    body: list[dict[str, Any]] = []
    attachments = attachments or []
    messages = pair_tool_messages(messages)
    for index, message in enumerate(messages):
        content: Any = message.content
        # 新格式把媒体归属到具体 user turn；attachments 参数仅兼容尚未迁移的调用方。
        message_attachments = [item.model_dump(by_alias=True) for item in message.attachments]
        if not message_attachments and attachments and index == len(messages) - 1 and message.role == "user":
            message_attachments = attachments
        if message_attachments and message.role == "user":
            content = [{"type": "text", "text": message.content}]
            content.extend(
                {
                    "type": "video_url" if str(attachment.get("type", "")).startswith("video/") else "image_url",
                    ("video_url" if str(attachment.get("type", "")).startswith("video/") else "image_url"): {"url": attachment["dataUrl"]},
                }
                for attachment in message_attachments
                if attachment.get("dataUrl")
            )
        if message.role == "tool":
            body.append({
                "role": "tool",
                "tool_call_id": message.meta.tool_call_id if message.meta else "",
                "content": message.content,
                "_message_id": message.id,
            })
            continue
        if message.tool_calls:
            # 历史工具调用必须以 provider 原生的 tool_calls 形式回放，模型才能
            # 把上一轮的 tool 结果和它自己发起的调用对上，而不是只看到一段总结。
            body.append({
                "role": "assistant",
                "content": message.content or None,
                "_message_id": message.id,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in message.tool_calls
                ],
            })
            continue
        body.append({"role": message.role, "content": content, "_message_id": message.id})
    if prompt is not None:
        reminder = prompt.context_message or ""
    else:
        reminder = _render_user_context(user_context or {})
    if context_summary:
        summary_block = (
            "<system-reminder>\n# conversation_summary\n"
            "This is continuity context from compacted earlier turns, not a new user request.\n\n"
            f"{context_summary}\n</system-reminder>"
        )
        reminder = "\n\n".join(item for item in (reminder, summary_block) if item)
    if working_set_context:
        working_block = (
            "<system-reminder>\n# post_compact_working_set\n"
            f"{working_set_context}\n</system-reminder>"
        )
        reminder = "\n\n".join(item for item in (reminder, working_block) if item)
    # 记忆和动态上下文作为独立的 user 消息插在历史之前，而不是拼进 system：
    # 这样 system 提示词保持稳定，可被 provider 端前缀缓存复用。
    return [
        {"role": "system", "content": prompt.system_prompt if prompt is not None else system_prompt},
        *([{"role": "user", "content": reminder, "_request_context": True}] if reminder else []),
        *body,
    ]


def estimate_text_tokens(value: str) -> int:
    """Estimate mixed ASCII/CJK token usage without a model-specific tokenizer."""

    if not value:
        return 0
    # 经验比例：英文约 4 字符 1 token，中日韩约 1.6 字符 1 token。
    # 这是刻意保守的估计，宁可高估触发压缩，也不要低估被 provider 拒绝。
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
            tool_calls = message.get("tool_calls")
        else:
            role = message.role
            content = message.content
            tool_calls = [call.model_dump() for call in message.tool_calls] or None
        rendered = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        # 工具调用的名称和参数同样占用输入额度，漏算会让长工具链低估预算。
        if tool_calls:
            rendered += json.dumps(tool_calls, ensure_ascii=False)
        # 每条消息额外加 4，覆盖 provider 消息封装（角色分隔符等）的固定开销。
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
