from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Small request identity object carried by ContextVar across async work."""

    request_id: str
    path: str = ""
    method: str = ""
    session_id: str | None = None
    run_id: str | None = None


# ContextVars are coroutine-local. When a request is moved onto the bounded
# agent thread pool, callers must copy the context and run the worker inside it.
_request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def new_request_context(path: str = "", method: str = "", request_id: str | None = None) -> RequestContext:
    return RequestContext(request_id=request_id or str(uuid.uuid4()), path=path, method=method)


def get_request_context() -> RequestContext | None:
    return _request_context.get()


def set_request_context(context: RequestContext) -> Token[RequestContext | None]:
    return _request_context.set(context)


def update_request_context(**changes: str | None) -> Token[RequestContext | None]:
    """Return a token so callers can always restore the previous context."""
    current = _request_context.get() or new_request_context()
    return _request_context.set(replace(current, **changes))


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _request_context.reset(token)
