"""Context budgeting, compaction, and runtime inspection."""

from backend.context.manager import (
    ContextBudget,
    ContextPlan,
    build_context_plan,
    compose_api_messages,
    estimate_message_tokens,
    estimate_text_tokens,
    pair_tool_messages,
)
from backend.context.budget import calculate_context_budget
from backend.context.compact import (
    CompactError,
    generate_compaction,
    group_api_rounds,
    is_context_length_error,
    sanitize_provider_messages,
)
from backend.context.models import ToolResultPolicy
from backend.context.tool_results import (
    limit_tool_result,
    microcompact,
    policy_for_tool,
)

__all__ = [
    "ContextBudget",
    "ContextPlan",
    "build_context_plan",
    "compose_api_messages",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "pair_tool_messages",
    "calculate_context_budget",
    "CompactError",
    "generate_compaction",
    "group_api_rounds",
    "is_context_length_error",
    "sanitize_provider_messages",
    "limit_tool_result",
    "microcompact",
    "policy_for_tool",
    "ToolResultPolicy",
]
