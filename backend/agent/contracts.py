"""无状态 Agent Backend 组件共用的一次 run 入参契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.api.schemas import ChatMessage


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """一次无状态执行的完整、已校验输入。

    prompt/上下文已在 Agent Backend 内拼装完毕；本结构只携带执行所需字段。
    """

    messages: list[ChatMessage]
    system_prompt: str
    user_context: dict[str, str]
    model_config: dict[str, Any]
    attachments: list[dict[str, Any]] = field(default_factory=list)
    mcp_server_ids: set[str] = field(default_factory=set)
    reasoning_effort: str | None = None
    loaded_memory_paths: list[str] = field(default_factory=list)
