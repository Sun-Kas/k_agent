from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.api.schemas import ChatMessage


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Complete, validated input for one stateless agent execution.

    Session lookup, prompt construction, skill selection and attachment
    validation must already be complete before this object crosses the agent
    backend boundary.
    """

    messages: list[ChatMessage]
    api_messages: list[dict[str, Any]]
    model_config: dict[str, Any]
    mcp_server_ids: set[str] = field(default_factory=set)
    reasoning_effort: str | None = None
    loaded_memory_paths: list[str] = field(default_factory=list)
