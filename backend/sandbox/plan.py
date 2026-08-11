"""把 Bash 工具调用规划成可沙箱的 `srt` 启动参数（若主机可用）。

pipeline：`cc_like.Bash` → `plan_bash_invocation`；`required` 模式无后端则失败，
`auto` 降级为非沙箱但必须把原因写回调用方，避免误判已隔离。
"""

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
    """沙箱为 required 但本机没有可用后端时抛出。"""


@dataclass(frozen=True, slots=True)
class BashInvocation:
    """Bash 工具实际应如何起进程。"""

    argv: list[str] | None
    """显式 argv（经 srt）；None 表示回落普通 shell。"""

    sandboxed: bool
    reason: str


def plan_bash_invocation(
    command: str,
    *,
    workspace_root: Path,
    settings: Settings,
    network_access: bool | None = None,
    full_access: bool = False,
) -> BashInvocation:
    """按 `bash_sandbox_mode` 决定本条命令走 srt 还是裸 shell。"""

    if full_access:
        # Full access is an explicit run-level user choice. Keep this separate
        # from global Settings so one conversation cannot weaken another run.
        return BashInvocation(None, False, "full access selected for this run")
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
    """把后端 Settings 译成 srt settings 文档。

    srt 默认禁写/禁网、默认可读；因此开放工作区写入，并关掉凭证目录与 `.env` 读取。
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
    payload: dict = {
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
    # Go CLIs on macOS (agently-cli / gh / …) verify TLS via Security.framework →
    # com.apple.trustd.agent. srt blocks that Mach service by default, which
    # surfaces as `x509: OSStatus -26276`. Re-enable only when configured.
    if settings.bash_sandbox_weaker_network_isolation:
        payload["enableWeakerNetworkIsolation"] = True
    return payload


def sanitize_srt_allowed_domains(
    configured: list[str] | tuple[str, ...],
) -> list[str]:
    """丢掉 srt 会拒的域名模式，避免校验失败导致整段沙箱被跳过。

    裸 `*`、`*.com` 这类过宽模式非法；忽略它们才能保住文件系统隔离。
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


def is_domain_allowed(hostname: str, configured: list[str] | tuple[str, ...]) -> bool:
    """Match one concrete hostname against the effective srt allowlist.

    Approval requests carry a hostname, never a URL or shell fragment. Keeping
    matching here aligned with srt wildcard semantics prevents an allowlisted
    transient failure from being misrepresented as a need for host access.
    """
    host = str(hostname).strip().lower().rstrip(".")
    if not host or "://" in host or "/" in host or ":" in host:
        return False
    for pattern in sanitize_srt_allowed_domains(configured):
        normalized = pattern.lower().rstrip(".")
        if normalized.startswith("*."):
            suffix = normalized[2:]
            if host.endswith(f".{suffix}"):
                return True
        elif host == normalized:
            return True
    return False


def _is_srt_allowed_domain(value: str) -> bool:
    """对齐 srt domainPatternSchema，拒绝已知非法条目。"""

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
    """把 srt settings 写到内容寻址的稳定路径。

    同内容并发 run 共享同一文件；内容变更换新文件，避免读到半写状态。
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
    """选择解释模型命令字符串的 shell。"""

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
