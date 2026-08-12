"""在模型上下文窗口内估算用量并压缩较旧对话。

pipeline：`OpenAIAgent.run_stream` 每轮先 `build_context_plan`，再在迭代前
`prune_old_tool_outputs`。启发式 token 估算留安全余量，避免估少触发 provider 报错。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any

from backend.api.schemas import ChatMessage


# 这些默认值只在模型配置缺字段时兜底，真实值应由 models.config.json 提供。
DEFAULT_CONTEXT_WINDOW = 1000_000
DEFAULT_MAX_OUTPUT_TOKENS = 50_000
# 安全余量吸收 token 估算误差：本模块用启发式估算而非精确 tokenizer，
# 估少了会直接触发 provider 的 context length 报错，所以宁可多留。
DEFAULT_SAFETY_TOKENS = 200_000
# 压缩后至少保留的最近消息数。低于这个数，模型会丢失当前任务的直接上下文
# （例如刚提的问题和刚回的工具结果），压缩反而比超预算更有害。
MIN_RECENT_MESSAGES = 6
MAX_SUMMARY_CHARS = 100_000
MAX_SUMMARY_MESSAGE_CHARS = 10_000
MAX_TOOL_CONTEXT_CHARS = 50_000


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
    """本轮选用的消息与摘要，附可审计的预算 breakdown。"""

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
    """构建上下文窗口计划；超预算时压缩较旧消息为摘要。"""

    budget = _context_budget(model_config)
    compacted = set(compacted_message_ids or [])
    active = pair_tool_messages(
        [message for message in messages if message.id not in compacted]
    )
    system_tokens = estimate_text_tokens(system_prompt)
    memory_tokens = estimate_text_tokens("\n".join(user_context.values()))
    summary_tokens = estimate_text_tokens(existing_summary)
    message_tokens = estimate_message_tokens(active)
    # 系统提示词、记忆、摘要和工具定义都是本轮无法裁剪的固定开销，
    # 只有对话消息可以被压缩，所以先扣掉固定部分再算消息可用额度。
    fixed_tokens = system_tokens + memory_tokens + summary_tokens + tool_definition_tokens
    # 即使固定开销已经吃满预算也保底给 1000，避免额度为 0 时把消息全部压缩掉。
    available_for_messages = max(1_000, budget.input_budget - fixed_tokens)
    should_compact = force_compact or message_tokens > available_for_messages

    summary = existing_summary
    newly_compacted: list[str] = []
    # 少于 3 条时无可压缩空间：压缩至少要留下最后一轮问答。
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
    # split 是保留原文的分界点：[:split] 进摘要，[split:] 保留原文。
    split = max(1, len(messages) - MIN_RECENT_MESSAGES)
    if not force and estimate_message_tokens(messages) <= message_token_budget:
        return existing_summary, [], messages
    # 消息总数还不到保留下限时，退化为只压缩最后两条之前的内容。
    if len(messages) <= MIN_RECENT_MESSAGES:
        split = max(1, len(messages) - 2)
    if not force:
        # 上面的分界点按条数切，往往会压缩掉本可以放下的消息。这里在预算内
        # 逐条往回扩大保留窗口：低于 70% 才继续尝试，一旦某次扩展会越过 78%
        # 就停手。两个阈值留出的间隙是给后续轮次的模型输出和新工具结果的，
        # 否则下一轮立刻又要压缩，形成反复摘要、上下文持续劣化。
        recent = messages[split:]
        while split > 0 and estimate_message_tokens(recent) < message_token_budget * 0.7:
            candidate = messages[split - 1 :]
            if estimate_message_tokens(candidate) > message_token_budget * 0.78:
                break
            split -= 1
            recent = candidate
    split = _tool_safe_split(messages, split)
    older = messages[:split]
    if not older:
        return existing_summary, [], messages
    summary = _merge_summary(existing_summary, older)
    return summary, [message.id for message in older], messages[split:]


def _tool_safe_split(messages: list[ChatMessage], split: int) -> int:
    """Move a compaction boundary forward past a tool result run.

    A tool message belongs to the assistant turn that requested it. Splitting
    between them would summarize the request while keeping an orphan result.
    """

    while split < len(messages) and messages[split].role == "tool":
        split += 1
    return split


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
    # 最近几条工具结果是模型当前推理的直接依据，必须保留原文；
    # 更早的可以换成占位符，模型需要时会重新调用工具。
    protected = set(tool_indexes[-keep_recent:])
    # 拷贝后再改，避免污染调用方持有的消息列表（主循环会跨迭代复用它）。
    pruned = [dict(message) for message in messages]
    for index in tool_indexes:
        # 从最老的开始裁，一旦降到阈值以下就停，尽量多保留原文。
        if total <= max_tool_chars or index in protected:
            continue
        content = str(pruned[index].get("content") or "")
        total -= len(content)
        pruned[index]["content"] = (
            f"[Older tool output cleared from active context: {len(content)} characters. "
            "Run the tool again if the exact output is needed.]"
        )
        # 占位符本身也占字符，计回总量后才能正确判断是否还需继续裁剪。
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
            })
            continue
        if message.tool_calls:
            # 历史工具调用必须以 provider 原生的 tool_calls 形式回放，模型才能
            # 把上一轮的 tool 结果和它自己发起的调用对上，而不是只看到一段总结。
            body.append({
                "role": "assistant",
                "content": message.content or None,
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
        body.append({"role": message.role, "content": content})
    reminder = _render_user_context(user_context)
    # 记忆和动态上下文作为独立的 user 消息插在历史之前，而不是拼进 system：
    # 这样 system 提示词保持稳定，可被 provider 端前缀缓存复用。
    return [
        {"role": "system", "content": system_prompt},
        *([{"role": "user", "content": reminder}] if reminder else []),
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


def _merge_summary(existing: str, messages: list[ChatMessage]) -> str:
    """把旧消息合并进上下文摘要。"""
    sections = [existing.strip()] if existing.strip() else []
    sections.append("# Compacted conversation")
    for message in messages:
        content = " ".join(message.content.split())
        if message.tool_calls:
            names = ", ".join(call.name for call in message.tool_calls)
            content = f"{content} (called {names})".strip()
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
    # 摘要会被反复追加，长会话下自身也可能膨胀。截断时保留尾部，
    # 因为越靠近当前轮次的内容对后续推理越有用。
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
