"""Environment scrubbing for sandboxed (and unsandboxed) Bash children."""

from __future__ import annotations

import os
from contextvars import ContextVar, Token

from backend.sandbox.constants import DEFAULT_ENV_ALLOWLIST


_tool_env_overrides: ContextVar[dict[str, str]] = ContextVar(
    "k_agent_tool_env_overrides", default={}
)


def set_tool_env_overrides(values: dict[str, str] | None) -> Token[dict[str, str]]:
    """Bind request-scoped env keys for Bash (shared project runtime, etc.)."""

    cleaned = {
        str(key): str(value)
        for key, value in (values or {}).items()
        if key and value is not None
    }
    return _tool_env_overrides.set(cleaned)


def reset_tool_env_overrides(token: Token[dict[str, str]]) -> None:
    """Restore the prior tool env override map."""

    _tool_env_overrides.reset(token)


def current_tool_env_overrides() -> dict[str, str]:
    """Return env overrides inherited by the current Agent run."""

    return dict(_tool_env_overrides.get())


def build_child_env(
    parent_env: dict[str, str] | None = None,
    *,
    extra_allow: tuple[str, ...] | list[str] = (),
) -> dict[str, str]:
    """Copy only allowlisted variables into the Bash child environment.

    Applied whether or not the OS sandbox is available: filesystem isolation
    does not stop `env` or `python -c 'import os; print(os.environ)'` from
    reading whatever the parent process handed over.
    """

    source = parent_env if parent_env is not None else os.environ
    overrides = current_tool_env_overrides()
    allowed = {name.upper() for name in DEFAULT_ENV_ALLOWLIST}
    allowed.update(name.upper() for name in extra_allow if name)
    allowed.update(key.upper() for key in overrides)
    # Locale categories beyond the short DEFAULT_ENV_ALLOWLIST (LC_TIME, …).
    for key in source:
        if key.upper().startswith("LC_"):
            allowed.add(key.upper())
    child = {
        key: value
        for key, value in source.items()
        if key.upper() in allowed and value is not None
    }
    # Request-scoped overrides win (e.g. shared PATH / npm prefix / cache).
    child.update(overrides)
    return child
