"""请求级标识：经 ContextVar 在 Access Layer 异步链路中传播。

在请求链路中的角色：HTTP middleware 为每个公开请求创建 `RequestContext`
（含 request_id）；网关进入 agent run 时补充 session_id / run_id；SSE
生成器跑在独立任务里，必须显式 `set_request_context` 复制上下文，否则
日志与后端 `X-Request-Id` 会丢失关联。

服务边界：仅 Access Layer 进程内使用；不跨服务序列化。ContextVar 是协程
本地的——切线程池或新建 Task 时调用方负责拷贝。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class RequestContext:
    """随异步工作携带的轻量请求身份（request_id / 路径 / 会话与 run）。"""

    request_id: str
    path: str = ""
    method: str = ""
    session_id: str | None = None
    run_id: str | None = None


# ContextVars 是协程本地的。请求若迁到有界线程池，调用方必须拷贝上下文
# 并在该 worker 内运行。
_request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def new_request_context(path: str = "", method: str = "", request_id: str | None = None) -> RequestContext:
    """创建带 request_id 的请求上下文；未传入时生成 UUID。"""
    return RequestContext(request_id=request_id or str(uuid.uuid4()), path=path, method=method)


def get_request_context() -> RequestContext | None:
    """读取当前协程中的请求上下文。"""
    return _request_context.get()


def set_request_context(context: RequestContext) -> Token[RequestContext | None]:
    """设置当前协程请求上下文，并返回用于 finally 恢复的 token。"""
    return _request_context.set(context)


def update_request_context(**changes: str | None) -> Token[RequestContext | None]:
    """在现有上下文上合并字段（如 session_id/run_id），返回可 reset 的 token。"""
    current = _request_context.get() or new_request_context()
    return _request_context.set(replace(current, **changes))


def reset_request_context(token: Token[RequestContext | None]) -> None:
    """用 token 恢复进入本请求段之前的上下文。"""
    _request_context.reset(token)
