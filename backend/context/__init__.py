"""Context budgeting, compaction, and runtime inspection."""

from backend.context.manager import (
    ContextBudget,
    ContextPlan,
    build_context_plan,
    compact_messages,
    compose_api_messages,
    estimate_message_tokens,
    estimate_text_tokens,
    pair_tool_messages,
    prune_old_tool_outputs,
)

__all__ = [
    "ContextBudget",
    "ContextPlan",
    "build_context_plan",
    "compact_messages",
    "compose_api_messages",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "pair_tool_messages",
    "prune_old_tool_outputs",
]
