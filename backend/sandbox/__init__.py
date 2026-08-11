"""OS-level sandboxing for the Bash tool via Anthropic's sandbox-runtime.

No sandbox primitive is portable, so `srt` is used as the platform abstraction:
Seatbelt on macOS, bubblewrap + seccomp on Linux. Windows is deliberately left
unsupported here — its `srt` backend is alpha and needs an elevated machine-wide
install that provisions a local user account and WFP filters, which is not a
reasonable prerequisite for running this project.

This package owns one decision: whether a given command can be sandboxed. It
never silently downgrades to a bare subprocess in `required` mode, because
silent degradation is the usual way a sandbox stops protecting anything.
"""

from backend.sandbox.constants import (
    DEFAULT_BASH_SANDBOX_ALLOWED_DOMAINS,
    DEFAULT_DENY_READ,
    DEFAULT_ENV_ALLOWLIST,
    SANDBOX_MODES,
)
from backend.sandbox.detect import (
    SandboxSupport,
    detect_support,
    reset_sandbox_detection,
)
from backend.sandbox.env import (
    build_child_env,
    current_tool_env_overrides,
    reset_tool_env_overrides,
    set_tool_env_overrides,
)
from backend.sandbox.guidance import (
    enrich_bash_result,
    notice_from_tool_result,
    sandbox_runtime_status,
)
from backend.sandbox.install import install_sandbox_runtime
from backend.sandbox.plan import (
    BashInvocation,
    SandboxUnavailable,
    build_settings_payload,
    is_domain_allowed,
    plan_bash_invocation,
)

__all__ = [
    "BashInvocation",
    "DEFAULT_BASH_SANDBOX_ALLOWED_DOMAINS",
    "DEFAULT_DENY_READ",
    "DEFAULT_ENV_ALLOWLIST",
    "SANDBOX_MODES",
    "SandboxSupport",
    "SandboxUnavailable",
    "build_child_env",
    "build_settings_payload",
    "is_domain_allowed",
    "current_tool_env_overrides",
    "detect_support",
    "enrich_bash_result",
    "install_sandbox_runtime",
    "notice_from_tool_result",
    "plan_bash_invocation",
    "reset_sandbox_detection",
    "reset_tool_env_overrides",
    "sandbox_runtime_status",
    "set_tool_env_overrides",
]
