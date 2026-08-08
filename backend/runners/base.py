"""Pluggable agent runners behind `/internal/agent/run`.

Each runner yields the same internal event dialect as `OpenAIAgent` so the
existing AG-UI translator stays the single wire-format boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from backend.api.schemas import ChatMessage
from backend.approvals import ApprovalBroker
from backend.config import Settings
from backend.mcp_tool import McpSessionPool
from backend.observability import LangfuseRuntime


# Extensible string kinds; built-ins are registered in `registry.py`.
AgentKind = str


@dataclass(frozen=True, slots=True)
class RunnerContext:
    """Per-run inputs shared by every agent backend implementation."""

    thread_id: str
    run_id: str
    request_id: str
    messages: list[ChatMessage]
    model_id: str | None
    mcp_servers: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    reasoning_effort: str | None
    attachments: list[dict[str, Any]]
    # Team runs receive a per-task workspace via workspaceDir. Conversation
    # runs leave it unset so the backend falls back to the session workspace.
    workspace_dir: Path | None = None
    team_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    settings: Settings | None = None
    mcp_pool: McpSessionPool | None = None
    langfuse: LangfuseRuntime | None = None
    logging_callback: Any | None = None
    approval_broker: ApprovalBroker | None = None


class AgentRunner(Protocol):
    """Stateless executor that streams internal agent events for one turn."""

    kind: AgentKind

    async def run_stream(self, ctx: RunnerContext) -> AsyncIterator[dict[str, Any]]:
        """Yield internal events consumed by `translate_agent_events`."""

        ...


RunnerFactory = Callable[[], AgentRunner]
