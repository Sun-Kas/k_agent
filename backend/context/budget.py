"""每次 Reason 前执行的上下文阈值计算。"""

from __future__ import annotations

from typing import Any

from backend.context.manager import estimate_message_tokens, estimate_text_tokens
from backend.context.models import ContextBudgetPayload, ContextDecision


def calculate_context_budget(
    *,
    model_config: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_definition_tokens: int = 0,
    latest_input_usage: int | None = None,
    usage_baseline_estimate: int | None = None,
) -> ContextDecision:
    """usage 优先；缺失时用保守估算，结果包含全部产品阈值。"""

    context_window = _positive(model_config.get("contextWindow"), 128_000)
    max_output = _positive(model_config.get("maxOutputTokens"), 8_192)
    safety = _non_negative(model_config.get("contextSafetyTokens"), 4_096)
    if context_window < max_output + safety + 8_000:
        raise ValueError(
            "contextWindow must be at least maxOutputTokens + contextSafetyTokens + 8000"
        )
    growth_reserve = max(safety, min(13_000, context_window // 10))
    auto_threshold = context_window - max_output - growth_reserve
    hard_threshold = context_window - max_output - 3_000
    warning_threshold = auto_threshold - min(20_000, int(context_window * 0.15))
    current_estimate = estimate_message_tokens(messages) + max(0, tool_definition_tokens)
    estimated = current_estimate
    if isinstance(latest_input_usage, int) and latest_input_usage > 0:
        # Provider usage 描述的是上一次成功请求。其后新产生的工具结果必须补回，
        # 不能因为拿到 usage 就把本轮增长量漏算。
        growth = (
            max(0, current_estimate - usage_baseline_estimate)
            if isinstance(usage_baseline_estimate, int)
            else 0
        )
        estimated = latest_input_usage + growth
    payload = ContextBudgetPayload(
        context_window=context_window,
        max_output_tokens=max_output,
        safety_tokens=safety,
        growth_reserve=growth_reserve,
        warning_threshold=warning_threshold,
        auto_compact_threshold=auto_threshold,
        hard_request_threshold=hard_threshold,
        estimated_input=estimated,
    )
    return ContextDecision(
        budget=payload,
        warning=estimated >= warning_threshold,
        needs_compact=estimated >= auto_threshold,
        hard_limit=estimated >= hard_threshold,
    )


def fixed_context_tokens(
    *, system_prompt: str, request_context: str, summary: str, tool_definition_tokens: int
) -> int:
    return (
        estimate_text_tokens(system_prompt)
        + estimate_text_tokens(request_context)
        + estimate_text_tokens(summary)
        + max(0, tool_definition_tokens)
    )


def _positive(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
