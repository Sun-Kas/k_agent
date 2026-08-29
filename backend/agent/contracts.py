"""无状态 Agent Backend 组件共用的一次 run 入参契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.api.schemas import ChatMessage


class CompiledPrompt(Protocol):
    """Provider-ready prompt surface consumed by the generic Agent core."""

    system_prompt: str
    context_message: str | None
    initial_memory_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """一次无状态执行的完整、已校验输入。

    prompt/上下文已在 Agent Backend 内拼装完毕；本结构只携带执行所需字段。
    """

    messages: list[ChatMessage]
    model_config: dict[str, Any]
    prompt: CompiledPrompt | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    mcp_server_ids: set[str] = field(default_factory=set)
    reasoning_effort: str | None = None
    permission_mode: str = "default"
    # Deprecated construction fields remain for narrow internal test fixtures;
    # production runners must pass the typed PromptBundle.
    system_prompt: str = ""
    user_context: dict[str, str] = field(default_factory=dict)
    loaded_memory_paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.prompt is None:
            object.__setattr__(
                self,
                "prompt",
                _LegacyCompiledPrompt(
                    self.system_prompt,
                    _legacy_context_message(self.user_context),
                ),
            )


@dataclass(frozen=True, slots=True)
class _LegacyCompiledPrompt:
    system_prompt: str
    context_message: str | None
    initial_memory_paths: tuple[str, ...] = ()


def _legacy_context_message(context: dict[str, str]) -> str | None:
    body = "\n\n".join(f"# {key}\n{value}" for key, value in context.items() if value)
    if not body:
        return None
    return f"<system-reminder>\n{body}\n</system-reminder>"
