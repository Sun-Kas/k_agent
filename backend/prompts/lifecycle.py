from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.memory import clear_memory_cache
from backend.prompts.sections import SECTION_CACHE


@dataclass
class PromptLifecycleState:
    generation: int = 0
    last_reset_at: str | None = None
    reason: str | None = None


STATE = PromptLifecycleState()


def reset_prompt_caches(reason: str = "manual") -> PromptLifecycleState:
    """Clear prompt and memory caches after /clear, /compact, config changes, or reconnects."""
    SECTION_CACHE.clear()
    clear_memory_cache()
    STATE.generation += 1
    STATE.reason = reason
    STATE.last_reset_at = datetime.now(timezone.utc).isoformat()
    return STATE


def prompt_lifecycle_state() -> PromptLifecycleState:
    return STATE

