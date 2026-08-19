"""进程级 Agent 单例注册表；实现按 agentKind 懒加载。"""

from __future__ import annotations

import threading
from collections.abc import Callable

from backend.runners.base import AgentKind, AgentRunner


AgentLoader = Callable[[], AgentRunner]


class RunnerRegistry:
    """缓存每种 Agent 的唯一实例，第一次 `get` 时才调用对应 loader。"""

    def __init__(self) -> None:
        self._loaders: dict[AgentKind, AgentLoader] = {}
        self._instances: dict[AgentKind, AgentRunner] = {}
        # `get()` 是同步方法，使用线程锁可覆盖同一进程内多线程同时首次读取；
        # loader 不得回调本 registry，避免在锁内递归加载。
        self._lock = threading.Lock()

    def register(self, kind: AgentKind, loader: AgentLoader) -> None:
        """注册懒加载入口；重复 kind 直接报错，避免悄悄覆盖已有单例。"""

        resolved = kind.strip()
        if not resolved:
            raise ValueError("Agent kind must not be empty")
        with self._lock:
            if resolved in self._loaders:
                raise ValueError(f"Agent kind {resolved!r} is already registered")
            self._loaders[resolved] = loader

    def get(self, kind: AgentKind | None) -> AgentRunner:
        """返回进程内唯一实例；未加载时只创建一次。"""

        resolved = (kind or "k_agent").strip() or "k_agent"
        with self._lock:
            runner = self._instances.get(resolved)
            if runner is not None:
                return runner
            loader = self._loaders.get(resolved)
            if loader is None:
                known = ", ".join(sorted(self._loaders)) or "(none)"
                raise ValueError(f"Unknown agentKind {resolved!r}; known: {known}")
            runner = loader()
            if runner.kind != resolved:
                raise ValueError(
                    f"Agent loader for {resolved!r} returned kind {runner.kind!r}"
                )
            self._instances[resolved] = runner
            return runner

    def kinds(self) -> list[AgentKind]:
        """列出已注册 kind；该操作不会触发 Agent 加载。"""

        with self._lock:
            return sorted(self._loaders)


def _load_k_agent() -> AgentRunner:
    """首次选择 k_agent 时才导入并构造默认 Agent。"""

    from backend.runners.k_agent import KAgentRunner

    return KAgentRunner()


def _load_codex() -> AgentRunner:
    """首次选择 Codex 时才导入并构造 Agent。"""

    from backend.runners.codex import CodexRunner

    return CodexRunner()


def _load_claude_code() -> AgentRunner:
    """首次选择 Claude Code 时才导入并构造 Agent。"""

    from backend.runners.claude_code import ClaudeCodeRunner

    return ClaudeCodeRunner()


# Registry 在模块导入时只登记轻量 loader；Agent 实例仍等到首次请求再创建。
_DEFAULT_REGISTRY = RunnerRegistry()
_DEFAULT_REGISTRY.register("k_agent", _load_k_agent)
_DEFAULT_REGISTRY.register("codex", _load_codex)
_DEFAULT_REGISTRY.register("claude_code", _load_claude_code)


def get_default_registry() -> RunnerRegistry:
    """返回当前 worker 进程唯一的默认 Registry。"""

    return _DEFAULT_REGISTRY
