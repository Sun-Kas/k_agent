"""给可信的进程内扩展贴声明式元数据。

装饰器 **只** 把 ``HookSpec`` 写到函数属性上，不会：
- 登记到模块/进程全局表
- 因 import 副作用自动挂上 Pipeline
- 扫描 Skill、环境变量或字符串路径

要生效必须在 ``builtins.build_k_agent_pipeline_definition``（Middleware）或
``bind_runtime(observers=...)``（Observer）里 **显式传入**。这避免不受信任代码
仅靠被 import 就进入执行链。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from backend.agent.hooks.types import AgentEventType, FailureMode, HookKind, HookSpec


CallableT = TypeVar("CallableT", bound=Callable[..., Any])
# 元数据属性名刻意带产品前缀，降低与第三方装饰器撞名的概率。
_HOOK_SPEC_ATTR = "__k_agent_hook_spec__"


def _decorate(
    kind: HookKind,
    *,
    order: int,
    name: str | None,
    failure_mode: FailureMode,
    event_type: AgentEventType | None = None,
    timeout_seconds: float | None = None,
) -> Callable[[CallableT], CallableT]:
    """附加不可变元数据；禁止同一函数叠两个 Hook 装饰器。"""

    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("Hook timeout_seconds must be greater than zero")

    def decorate(func: CallableT) -> CallableT:
        if hasattr(func, _HOOK_SPEC_ATTR):
            raise ValueError(f"Hook {func.__name__!r} is already decorated")
        resolved_name = (name or func.__name__).strip()
        if not resolved_name:
            raise ValueError("Hook name must not be empty")
        setattr(
            func,
            _HOOK_SPEC_ATTR,
            HookSpec(
                kind=kind,
                name=resolved_name,
                order=order,
                failure_mode=failure_mode,
                event_type=event_type,
                timeout_seconds=timeout_seconds,
            ),
        )
        return func

    return decorate


def hook_spec(value: Any) -> HookSpec | None:
    """只读取出装饰器元数据，不触发发现、import 或注册。"""

    spec = getattr(value, _HOOK_SPEC_ATTR, None)
    return spec if isinstance(spec, HookSpec) else None


def observe(
    event_type: AgentEventType,
    *,
    order: int = 100,
    name: str | None = None,
    timeout_seconds: float | None = None,
) -> Callable[[CallableT], CallableT]:
    """声明单事件 Observer。固定 fail-open：超时/抛错不得打断 Agent。"""

    return _decorate(
        HookKind.OBSERVER,
        order=order,
        name=name,
        failure_mode=FailureMode.OPEN,
        event_type=event_type,
        timeout_seconds=timeout_seconds,
    )


def before_agent(*, order: int = 100, name: str | None = None):
    """Agent 循环进入前。失败视为 run 未开始（fail-closed）。"""

    return _decorate(
        HookKind.BEFORE_AGENT,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def after_agent(*, order: int = 100, name: str | None = None):
    """最终结果已发出 ``AgentCompleted`` 之后。编译时按 order **逆序**。"""

    return _decorate(
        HookKind.AFTER_AGENT,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def before_model(*, order: int = 100, name: str | None = None):
    """每次模型调用前。可通过 ``state['model_request']`` 改逻辑请求。"""

    return _decorate(
        HookKind.BEFORE_MODEL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def after_model(*, order: int = 100, name: str | None = None):
    """模型流成功结束后。编译时按 order **逆序**。"""

    return _decorate(
        HookKind.AFTER_MODEL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def wrap_model_call(*, order: int = 100, name: str | None = None):
    """洋葱包裹模型流。较小 order 在外层；可见 delta 发出后禁止重试。"""

    return _decorate(
        HookKind.WRAP_MODEL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )


def wrap_tool_call(*, order: int = 100, name: str | None = None):
    """洋葱包裹工具调用。内层永远是 sealed preflight→execute，拆不掉安全门。"""

    return _decorate(
        HookKind.WRAP_TOOL,
        order=order,
        name=name,
        failure_mode=FailureMode.CLOSED,
    )
