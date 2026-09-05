"""Backend 内部上下文压缩协议模型；不拥有任何会话文件。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ContextBudgetPayload:
    context_window: int
    max_output_tokens: int
    safety_tokens: int
    growth_reserve: int
    warning_threshold: int
    auto_compact_threshold: int
    hard_request_threshold: int
    estimated_input: int

    def as_dict(self) -> dict[str, int]:
        return {
            "contextWindow": self.context_window,
            "maxOutputTokens": self.max_output_tokens,
            "contextSafetyTokens": self.safety_tokens,
            "growthReserve": self.growth_reserve,
            "warningThreshold": self.warning_threshold,
            "autoCompactThreshold": self.auto_compact_threshold,
            "hardRequestThreshold": self.hard_request_threshold,
            "estimatedInput": self.estimated_input,
        }


@dataclass(frozen=True, slots=True)
class ContextDecision:
    budget: ContextBudgetPayload
    warning: bool
    needs_compact: bool
    hard_limit: bool


@dataclass(frozen=True, slots=True)
class ToolResultPolicy:
    mode: Literal["rerunnable", "receipt", "retain"] = "retain"
    max_result_chars: int = 50_000


@dataclass(frozen=True, slots=True)
class MicrocompactResult:
    messages: list[dict[str, Any]]
    replacements: list[dict[str, Any]] = field(default_factory=list)
    before_chars: int = 0
    after_chars: int = 0


@dataclass(frozen=True, slots=True)
class CompactResult:
    proposal: dict[str, Any]
    continuation_checkpoint: dict[str, Any] | None
    remaining_messages: list[dict[str, Any]]
