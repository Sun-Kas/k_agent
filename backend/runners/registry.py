"""agentKind → Runner 工厂表；进程内注册，Runner 本身仍按请求新建、无状态。"""

from __future__ import annotations

from backend.runners.base import AgentKind, AgentRunner, RunnerFactory
from backend.runners.claude_code import ClaudeCodeRunner
from backend.runners.codex import CodexRunner
from backend.runners.k_agent import KAgentRunner


class RunnerRegistry:
    """进程级 kind 查找表；`get` 每次调用 factory()，避免跨 run 复用实例状态。"""

    def __init__(self) -> None:
        self._factories: dict[AgentKind, RunnerFactory] = {}

    def register(self, kind: AgentKind, factory: RunnerFactory) -> None:
        self._factories[kind] = factory

    def get(self, kind: AgentKind | None) -> AgentRunner:
        resolved = (kind or "k_agent").strip() or "k_agent"
        factory = self._factories.get(resolved)
        if factory is None:
            known = ", ".join(sorted(self._factories)) or "(none)"
            raise ValueError(f"Unknown agentKind {resolved!r}; known: {known}")
        return factory()

    def kinds(self) -> list[AgentKind]:
        return sorted(self._factories)


def build_default_registry() -> RunnerRegistry:
    """注册内置 k_agent / codex / claude_code 三种 Runner。"""
    registry = RunnerRegistry()
    registry.register("k_agent", KAgentRunner)
    registry.register("codex", CodexRunner)
    registry.register("claude_code", ClaudeCodeRunner)
    return registry
