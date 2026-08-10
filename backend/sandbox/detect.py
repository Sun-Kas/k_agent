"""探测本机能否运行 Anthropic sandbox-runtime（`srt`）。"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SandboxSupport:
    """本机是否具备沙箱能力；不可用时带上原因。"""

    available: bool
    reason: str


_support_cache: SandboxSupport | None = None


def reset_sandbox_detection() -> None:
    """丢弃缓存的探测结果，供测试与重配重新检测。"""

    global _support_cache
    _support_cache = None


def detect_support(sandbox_command: str) -> SandboxSupport:
    """进程内只探测一次：`srt` 与平台前置（Linux 还需 bwrap）是否就绪。"""

    global _support_cache
    if _support_cache is not None:
        return _support_cache
    _support_cache = _detect_support_uncached(sandbox_command)
    return _support_cache


def _detect_support_uncached(sandbox_command: str) -> SandboxSupport:
    """检查平台与必需二进制，不写缓存。"""

    if sys.platform == "win32":
        return SandboxSupport(
            False,
            "native Windows is not supported; run the backend under WSL2 to get "
            "the Linux sandbox backend",
        )
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        return SandboxSupport(False, f"unsupported platform {sys.platform!r}")
    if shutil.which(sandbox_command) is None:
        return SandboxSupport(
            False,
            f"{sandbox_command!r} not found on PATH; install it with "
            "`npm install -g @anthropic-ai/sandbox-runtime`",
        )
    # bubblewrap is the Linux filesystem backend and is not bundled with the
    # npm package, unlike macOS where sandbox-exec ships with the OS.
    if sys.platform.startswith("linux") and shutil.which("bwrap") is None:
        return SandboxSupport(
            False, "'bwrap' (bubblewrap) not found on PATH; required by srt on Linux"
        )
    return SandboxSupport(True, "ok")
