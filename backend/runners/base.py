"""可插拔 Agent Runner 契约：`/internal/agent/run` 按 agentKind 调度。

每种 Runner 必须产出与 `OpenAIAgent` 相同的内部事件方言，
这样 `agui.translate_agent_events` 仍是唯一的线协议边界。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    """创建 Runtime 所需的只读请求输入与进程级依赖。"""

    thread_id: str
    run_id: str
    request_id: str
    messages: list[ChatMessage]
    model_id: str | None
    mcp_servers: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    reasoning_effort: str | None
    attachments: list[dict[str, Any]]
    # 标准 AG-UI Resume 决议及 Access Layer 可信 checkpoint；Runner 不读磁盘。
    resume: list[dict[str, Any]] = field(default_factory=list)
    resume_checkpoints: list[dict[str, Any]] = field(default_factory=list)
    # Access Layer 必须显式下发；Runner 不得通过 thread_id 推导会话存储路径。
    workspace_dir: Path | None = None
    team_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    settings: Settings | None = None
    mcp_pool: McpSessionPool | None = None
    langfuse: LangfuseRuntime | None = None
    logging_observer: Any | None = None
    approval_broker: ApprovalBroker | None = None


class AgentRunner(Protocol):
    """进程内复用的无状态 Agent；Runtime 只存在于函数调用作用域。"""

    kind: AgentKind

    def run_stream(self, context: RunnerContext) -> AsyncIterator[dict[str, Any]]:
        """流式转发本轮 Runtime 结果。"""

        ...
