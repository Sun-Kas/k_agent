"""Install Anthropic sandbox-runtime after an explicit user confirmation."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from typing import Any

from backend.sandbox.detect import detect_support, reset_sandbox_detection
from backend.sandbox.env import build_child_env
from backend.sandbox.guidance import (
    MACOS_RIPGREP_COMMAND,
    MANUAL_INSTALL_COMMAND,
    install_guidance_payload,
)


logger = logging.getLogger(__name__)

SANDBOX_PACKAGE = "@anthropic-ai/sandbox-runtime"
DEFAULT_INSTALL_TIMEOUT_SECONDS = 300.0


def build_install_env(parent_env: dict[str, str] | None = None) -> dict[str, str]:
    """Allow npm/Homebrew proxy and prefix vars while still stripping secrets."""

    source = parent_env if parent_env is not None else os.environ
    env = build_child_env(source)
    for key, value in source.items():
        upper = key.upper()
        if value is None:
            continue
        if (
            upper.startswith("NPM_")
            or key.startswith("npm_config_")
            or upper.startswith("HOMEBREW_")
            or upper
            in {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "NODE_PATH",
                "NODE_OPTIONS",
                "SSL_CERT_FILE",
                "REQUESTS_CA_BUNDLE",
                "CURL_CA_BUNDLE",
            }
        ):
            env[key] = value
    return env


async def install_sandbox_runtime(
    *,
    confirmed: bool,
    sandbox_command: str = "srt",
    timeout_seconds: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Install srt (and macOS ripgrep when missing) only after confirmation.

    This path intentionally bypasses the Bash sandbox wrapper: the sandbox
    binary is what we are trying to obtain, so requiring it would deadlock.
    """

    if not confirmed:
        return {
            "ok": False,
            "error": (
                "InstallSandbox requires confirmed=true after the user explicitly "
                "agrees in chat. Do not install without confirmation."
            ),
            "available": False,
            **install_guidance_payload(reason="user confirmation required"),
        }

    if sys.platform == "win32":
        return {
            "ok": False,
            "error": (
                "native Windows is not supported; run the backend under WSL2, "
                f"then install with `{MANUAL_INSTALL_COMMAND}`"
            ),
            "available": False,
        }

    if shutil.which("npm") is None:
        return {
            "ok": False,
            "error": "npm not found on PATH; install Node.js first, then retry",
            "available": False,
            "manualInstallCommand": MANUAL_INSTALL_COMMAND,
        }

    steps: list[dict[str, Any]] = []
    env = build_install_env()
    srt_step = await _run_install_command(
        MANUAL_INSTALL_COMMAND,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    steps.append(srt_step)
    if not srt_step["ok"]:
        return {
            "ok": False,
            "error": srt_step.get("error") or "failed to install sandbox-runtime",
            "available": False,
            "steps": steps,
            "manualInstallCommand": MANUAL_INSTALL_COMMAND,
        }

    if sys.platform == "darwin" and shutil.which("rg") is None:
        if shutil.which("brew") is None:
            steps.append(
                {
                    "ok": False,
                    "command": MACOS_RIPGREP_COMMAND,
                    "error": "brew not found; install ripgrep manually for srt deny-path scans",
                }
            )
        else:
            steps.append(
                await _run_install_command(
                    MACOS_RIPGREP_COMMAND,
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
            )

    reset_sandbox_detection()
    support = detect_support(sandbox_command)
    return {
        "ok": support.available,
        "available": support.available,
        "reason": support.reason,
        "command": sandbox_command,
        "steps": steps,
        "error": None if support.available else support.reason,
        "manualInstallCommand": MANUAL_INSTALL_COMMAND,
    }


async def _run_install_command(
    command: str,
    *,
    env: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one install shell command with a bounded timeout."""

    logger.info("Installing sandbox dependency: %s", command)
    process = await asyncio.create_subprocess_shell(
        command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return {
            "ok": False,
            "command": command,
            "error": f"install timed out after {timeout_seconds}s",
        }
    stdout = stdout_bytes.decode(errors="replace")[-4000:]
    stderr = stderr_bytes.decode(errors="replace")[-4000:]
    return {
        "ok": process.returncode == 0,
        "command": command,
        "exitCode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "error": None if process.returncode == 0 else (stderr.strip() or stdout.strip() or "install failed"),
    }
