"""Shared sandbox modes and default allow/deny lists."""

from __future__ import annotations

SANDBOX_MODES = ("off", "auto", "required")

# Credential stores that a coding agent never needs to read. Denying them is
# cheap insurance: srt allows reads everywhere by default.
DEFAULT_DENY_READ = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.kube",
    "~/.config/gcloud",
    "~/.npmrc",
    "~/.pypirc",
    "~/.netrc",
    "~/.docker/config.json",
)

# Exact names the child may inherit. Keys and secrets stay in the parent process;
# the model can always run `env`, so a deny-list of secret names is not enough.
DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "COLORTERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TMPDIR",
    "TMP",
    "TEMP",
    "TZ",
    "DISPLAY",
    "SSH_AUTH_SOCK",
    "XPC_FLAGS",
    "XPC_SERVICE_NAME",
)
