"""Internal request contract shared by the stateless Agent Backend components."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.api.schemas import ChatMessage


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Complete, validated input for one stateless agent execution.

    All prompt/context preparation has already happened inside Agent Backend.
    """

    messages: list[ChatMessage]
    system_prompt: str
    user_context: dict[str, str]
    model_config: dict[str, Any]
    attachments: list[dict[str, Any]] = field(default_factory=list)
    mcp_server_ids: set[str] = field(default_factory=set)
    reasoning_effort: str | None = None
    loaded_memory_paths: list[str] = field(default_factory=list)
