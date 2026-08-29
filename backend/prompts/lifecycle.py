"""动态拼装 prompt 的集中式缓存失效状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.memory import clear_memory_cache


@dataclass
class PromptLifecycleState:
    """prompt/memory 缓存代数：generation 递增表示前端应视为「上下文代际」已变。"""
    generation: int = 0
    last_reset_at: str | None = None
    reason: str | None = None


STATE = PromptLifecycleState()


def reset_prompt_caches(reason: str = "manual") -> PromptLifecycleState:
    """在 /clear、/compact、配置变更或重连后清空 prompt 与 memory 缓存。"""
    clear_memory_cache()
    STATE.generation += 1
    STATE.reason = reason
    STATE.last_reset_at = datetime.now(timezone.utc).isoformat()
    return STATE


def prompt_lifecycle_state() -> PromptLifecycleState:
    """返回进程内当前的 prompt 生命周期快照（只读观察）。"""
    return STATE
