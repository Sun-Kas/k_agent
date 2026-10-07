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


# 每个 Task 各自一份「当前值」，set 是替换这份当前值，不是压栈。
# 旧 RequestContext 对象还在，但 get() 只能读到最新那份。
# Token 记着「这次 set 之前的当前值」，reset 才能恢复。
# 请求若迁到线程池或新建 Task，调用方必须自己拷贝后再 set。
_request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def new_request_context(path: str = "", method: str = "", request_id: str | None = None) -> RequestContext:
    """创建带 request_id 的请求上下文；未传入时生成 UUID。"""
    return RequestContext(request_id=request_id or str(uuid.uuid4()), path=path, method=method)


def get_request_context() -> RequestContext | None:
    """读取当前协程中的请求上下文。"""
    return _request_context.get()


def set_request_context(context: RequestContext) -> Token[RequestContext | None]:
    """写入当前协程的 RequestContext。

    Token 不是这份 context 的拷贝，而是 set 之前那一格的书签。
    中间件 set 得到 token_mw（底下是 None）；网关再 update 得到
    token_gw（底下是中间件那层）。finally 必须用对应 token reset，
    才能一层层揭回去，不能拿最新值当「清空」。
    """
    return _request_context.set(context)


def update_request_context(**changes: str | None) -> Token[RequestContext | None]:
    """在现有上下文上合并字段（如 session_id/run_id），返回可 reset 的 token。"""
    current = _request_context.get() or new_request_context()
    return _request_context.set(replace(current, **changes))


def reset_request_context(token: Token[RequestContext | None]) -> None:
    """按 token 把 ContextVar 恢复成那次 set/update 之前的值。"""
    _request_context.reset(token)
