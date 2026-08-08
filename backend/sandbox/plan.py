"""Turn Bash tool calls into sandboxed `srt` invocations when possible."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backend.config import Settings
from backend.sandbox.constants import DEFAULT_DENY_READ
from backend.sandbox.detect import detect_support
from backend.storage import write_json_atomic


logger = logging.getLogger(__name__)


class SandboxUnavailable(RuntimeError):
    """Raised when a sandbox is required but no usable backend exists."""


@dataclass(frozen=True, slots=True)
class BashInvocation:
    """How the Bash tool should actually spawn a command."""

    argv: list[str] | None
    """Explicit argv for a sandboxed spawn, or None to fall back to the shell."""

    sandboxed: bool
    reason: str


def plan_bash_invocation(
    command: str,
    *,
    workspace_root: Path,
    settings: Settings,
    network_access: bool | None = None,
) -> BashInvocation:
    """Decide how to run one Bash command under the configured sandbox mode."""

    mode = settings.bash_sandbox_mode
    if mode == "off":
        return BashInvocation(None, False, "sandbox disabled by configuration")

    support = detect_support(settings.bash_sandbox_command)
    if not support.available:
        if mode == "required":
            raise SandboxUnavailable(support.reason)
        # `auto` keeps the tool usable on hosts without a backend, but the caller
        # surfaces the reason so an unsandboxed run is never mistaken for a
        # sandboxed one.
        logger.info("Bash sandbox unavailable, running unsandboxed: %s", support.reason)
        return BashInvocation(None, False, support.reason)

    settings_path = _materialize_settings(
        workspace_root=workspace_root,
        settings=settings,
        network_access=network_access,
    )
    argv = [
        settings.bash_sandbox_command,
        "--settings",
        str(settings_path),
        _shell_path(),
        "-c",
        command,
    ]
    return BashInvocation(argv, True, "ok")


def build_settings_payload(
    *,
    workspace_root: Path,
    settings: Settings,
    network_access: bool | None = None,
) -> dict:
    """Translate backend settings into an srt settings document.

    srt denies writes and network by default and allows reads by default, so the
    workspace is opened for writing while credential stores are closed for
    reading.
    """

    allow_write = [str(workspace_root), tempfile.gettempdir()]
    allow_write.extend(_expand_paths(settings.bash_sandbox_write_paths))
    deny_read = _expand_paths(DEFAULT_DENY_READ)
    deny_read.extend(_expand_paths(settings.bash_sandbox_deny_read))
    # The project's own .env holds the model API keys that this agent runs on.
    deny_read.append(str(workspace_root / ".env"))
    enabled = (
        settings.network_access_default if network_access is None else network_access
    )
    return {
        "filesystem": {
            "denyRead": _dedupe(deny_read),
            "allowRead": [],
            "allowWrite": _dedupe(allow_write),
            "denyWrite": [str(workspace_root / ".env")],
        },
        "network": {
            "allowedDomains": (
                sanitize_srt_allowed_domains(settings.bash_sandbox_allowed_domains)
                if enabled
                else []
            ),
            "deniedDomains": [],
            "allowLocalBinding": False,
        },
    }


def sanitize_srt_allowed_domains(
    configured: list[str] | tuple[str, ...],
) -> list[str]:
    """Drop patterns srt rejects so Bash stays sandboxed.

    Bare `*` and TLD-wide wildcards like `*.com` are invalid in srt's
    allowedDomains. Ignoring them keeps the filesystem sandbox running instead
    of failing config validation or skipping srt.
    """

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in configured:
        item = str(raw).strip()
        if not item or item in seen:
            continue
        if not _is_srt_allowed_domain(item):
            logger.warning(
                "Ignoring invalid Bash sandbox domain %r "
                "(srt requires concrete hosts like example.com or *.example.com)",
                item,
            )
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def _is_srt_allowed_domain(value: str) -> bool:
    """Mirror srt's domainPatternSchema enough to reject known-invalid entries."""

    if "://" in value or "/" in value or ":" in value:
        return False
    if value == "localhost":
        return True
    if value.startswith("*."):
        domain = value[2:]
        if not domain or domain.startswith(".") or domain.endswith("."):
            return False
        parts = domain.split(".")
        # srt rejects overly broad patterns such as *.com
        return len(parts) >= 2 and all(parts)
    if "*" in value:
        return False
    return "." in value and not value.startswith(".") and not value.endswith(".")


def _materialize_settings(
    *, workspace_root: Path, settings: Settings, network_access: bool | None
) -> Path:
    """Write the srt settings document to a stable, content-addressed path.

    Keying the filename by content means concurrent runs with identical settings
    share one file and a settings change produces a new one, so a command never
    reads a file that another run is midway through rewriting.
    """

    payload = build_settings_payload(
        workspace_root=workspace_root,
        settings=settings,
        network_access=network_access,
    )
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / "k_agent_sandbox"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"srt-{digest}.json"
    if not path.exists():
        write_json_atomic(path, payload)
    return path


def _shell_path() -> str:
    """Pick the shell used to interpret the model's command string."""

    for candidate in ("/bin/bash", "/bin/sh"):
        if Path(candidate).exists():
            return candidate
    return os.environ.get("SHELL") or "/bin/sh"


def _expand_paths(paths: tuple[str, ...] | list[str]) -> list[str]:
    """Resolve `~` locally rather than relying on srt's path syntax per platform."""

    return [str(Path(path).expanduser()) for path in paths if path]


def _dedupe(paths: list[str]) -> list[str]:
    """Drop duplicates while keeping the configured ordering readable."""

    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result
