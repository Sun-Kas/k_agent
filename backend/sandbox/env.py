"""Environment scrubbing for sandboxed (and unsandboxed) Bash children."""

from __future__ import annotations

import os

from backend.sandbox.constants import DEFAULT_ENV_ALLOWLIST


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
    allowed = {name.upper() for name in DEFAULT_ENV_ALLOWLIST}
    allowed.update(name.upper() for name in extra_allow if name)
    # Locale categories beyond the short DEFAULT_ENV_ALLOWLIST (LC_TIME, …).
    for key in source:
        if key.upper().startswith("LC_"):
            allowed.add(key.upper())
    return {
        key: value
        for key, value in source.items()
        if key.upper() in allowed and value is not None
    }
