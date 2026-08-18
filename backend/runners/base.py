"""可插拔 Agent Runner 契约：`/internal/agent/run` 按 agentKind 调度。

每种 Runner 必须产出与 `OpenAIAgent` 相同的内部事件方言，
这样 `agui.translate_agent_events` 仍是唯一的线协议边界。
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


# 可扩展的字符串 kind；内置实现在 `registry.py` 注册。
AgentKind = str

'''
frozen=True = 不能改已有字段

slots=True = 不能随便加新字段 + 更省内存
'''
@dataclass(frozen=True, slots=True)
class RunnerContext:
    """单次 run 的共享入参；所有后端实现只读这份上下文，不跨请求保留状态。"""

    thread_id: str
    run_id: str
    request_id: str
    messages: list[ChatMessage]
    model_id: str | None
    mcp_servers: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    reasoning_effort: str | None
    attachments: list[dict[str, Any]]
    # Team 跑法经 workspaceDir 下发任务级目录；普通对话留空，回落 session workspace。
    workspace_dir: Path | None = None
    team_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    settings: Settings | None = None
    mcp_pool: McpSessionPool | None = None
    langfuse: LangfuseRuntime | None = None
    logging_callback: Any | None = None
    approval_broker: ApprovalBroker | None = None


class AgentRunner(Protocol):
    """无状态执行器：吃掉 RunnerContext，流式产出内部 `{type,payload}` 事件。"""

    kind: AgentKind

    async def run_stream(self, ctx: RunnerContext) -> AsyncIterator[dict[str, Any]]:
        """产出供 `translate_agent_events` 消费的内部事件。"""

        ...


RunnerFactory = Callable[[], AgentRunner]
