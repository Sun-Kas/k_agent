"""User- and model-facing guidance when the Bash sandbox cannot be used."""

from __future__ import annotations

import json
import sys
from typing import Any

from backend.config import Settings
from backend.sandbox.detect import SandboxSupport, detect_support


MANUAL_INSTALL_COMMAND = "npm install -g @anthropic-ai/sandbox-runtime"
MACOS_RIPGREP_COMMAND = "brew install ripgrep"
LINUX_DEPS_HINT = "Linux 还需安装 bubblewrap 与 socat（如 apt install bubblewrap socat）"
WINDOWS_HINT = "原生 Windows 不支持 Bash 沙箱；请在 WSL2 中运行后端后再安装 srt"


def _platform_label() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    if sys.platform == "win32":
        return "Windows"
    return sys.platform


def _is_unsupported_platform(reason: str) -> bool:
    lowered = reason.lower()
    return "windows" in lowered or "unsupported platform" in lowered


def sandbox_runtime_status(settings: Settings) -> dict[str, Any]:
    """Snapshot used by health endpoints and UI badges."""

    platform = _platform_label()
    if settings.bash_sandbox_mode == "off":
        return {
            "available": False,
            "mode": settings.bash_sandbox_mode,
            "command": settings.bash_sandbox_command,
            "reason": "sandbox disabled by configuration",
            "needsInstall": False,
            "platform": platform,
            "userSummary": "Bash 沙箱已关闭（BASH_SANDBOX_MODE=off）",
        }
    support = detect_support(settings.bash_sandbox_command)
    unsupported = _is_unsupported_platform(support.reason)
    needs_install = not support.available and not unsupported
    if support.available:
        user_summary = f"Bash 沙箱就绪（{platform}）"
    elif unsupported:
        user_summary = WINDOWS_HINT if "windows" in support.reason.lower() else support.reason
    elif "bwrap" in support.reason:
        user_summary = (
            f"已检测到环境问题：{support.reason}。{LINUX_DEPS_HINT}。"
            f"srt 本身可执行：{MANUAL_INSTALL_COMMAND}"
        )
    else:
        user_summary = (
            f"Bash 沙箱未就绪（{support.reason}）。"
            f"可手动安装：{MANUAL_INSTALL_COMMAND}；"
            "或在对话里确认后让助手调用 InstallSandbox 安装。"
        )
    return {
        "available": support.available,
        "mode": settings.bash_sandbox_mode,
        "command": settings.bash_sandbox_command,
        "reason": support.reason,
        "needsInstall": needs_install,
        "platform": platform,
        "userSummary": user_summary,
        "manualInstallCommand": None if unsupported else MANUAL_INSTALL_COMMAND,
        "agentInstallTool": None if unsupported else "InstallSandbox",
    }


def needs_install_guidance(*, mode: str, sandboxed: bool, reason: str) -> bool:
    """True when the host should be nudged about sandbox setup (not when mode is off)."""

    if mode == "off" or sandboxed:
        return False
    if reason == "sandbox disabled by configuration":
        return False
    return True


def _platform_steps(reason: str) -> list[str]:
    """Concrete next steps tailored to the host OS and failure reason."""

    if _is_unsupported_platform(reason):
        return [
            WINDOWS_HINT,
            "在 WSL2 内安装 Node.js 后执行："
            f"`{MANUAL_INSTALL_COMMAND}`，并安装 bubblewrap/socat",
        ]
    steps = [
        "流程：① 助手说明沙箱未就绪 → ② 你确认是否安装 → "
        "③ 确认后助手调用 InstallSandbox，或你手动安装",
        f"手动安装：`{MANUAL_INSTALL_COMMAND}`",
        "对话安装：回复确认（例如「可以，帮我安装沙箱」），"
        "助手再调用 `InstallSandbox`（confirmed=true）；未确认不会安装",
    ]
    if sys.platform == "darwin":
        steps.append(f"macOS 建议同时安装 ripgrep：`{MACOS_RIPGREP_COMMAND}`")
    if sys.platform.startswith("linux") or "bwrap" in reason:
        steps.append(LINUX_DEPS_HINT)
    return steps


def install_guidance_payload(*, reason: str) -> dict[str, Any]:
    """Structured install hints embedded in Bash / InstallSandbox tool results."""

    unsupported = _is_unsupported_platform(reason)
    steps = _platform_steps(reason)
    user_message = (
        f"【Bash 沙箱】当前不可用：{reason}\n"
        + "\n".join(f"- {step}" for step in steps)
    )
    if unsupported:
        agent_hint = (
            "向用户完整说明："
            f"{WINDOWS_HINT}。"
            "不要调用 InstallSandbox；请用户改到 WSL2 后再装 srt。"
        )
    else:
        agent_hint = (
            "向用户完整说明沙箱未就绪的原因、平台限制，以及两条路径："
            f"（1）手动执行 `{MANUAL_INSTALL_COMMAND}`；"
            "（2）用户在对话中明确确认后，再调用 InstallSandbox(confirmed=true)。"
            "未得到确认前禁止安装。说明时用简短中文，直接面向用户。"
        )
    return {
        "installGuidance": user_message,
        "userMessage": user_message,
        "manualInstallCommand": None if unsupported else MANUAL_INSTALL_COMMAND,
        "agentInstallHint": agent_hint,
        "installSteps": steps,
        "platform": _platform_label(),
        "unsupportedPlatform": unsupported,
    }


def user_status_message(*, reason: str, mode: str) -> str:
    """Short Chinese status line for the AG-UI status pill."""

    if _is_unsupported_platform(reason):
        return f"Bash 沙箱不可用：{WINDOWS_HINT}"
    prefix = (
        f"Bash 沙箱不可用（{reason}）"
        if mode == "required"
        else f"Bash 沙箱未就绪（{reason}），本轮以非沙箱方式执行"
    )
    return (
        f"{prefix}。可手动安装：{MANUAL_INSTALL_COMMAND}；"
        "或在对话中确认后让助手安装（InstallSandbox）。"
    )


def notice_from_tool_result(
    tool_name: str,
    result: str,
    *,
    settings: Settings,
) -> str | None:
    """Return a one-shot user status message for sandbox-related tool outcomes."""

    if tool_name not in {"Bash", "InstallSandbox"}:
        return None
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    if tool_name == "InstallSandbox":
        if payload.get("ok") is True and payload.get("available") is True:
            return "Bash 沙箱已安装并可用。后续 Bash 命令将优先在沙箱中执行。"
        if payload.get("ok") is False and payload.get("error"):
            error = str(payload["error"])
            if _is_unsupported_platform(error):
                return f"Bash 沙箱安装未执行：{WINDOWS_HINT}"
            return (
                f"Bash 沙箱安装失败：{error}。"
                f"也可手动执行：{MANUAL_INSTALL_COMMAND}"
            )
        return None

    sandboxed = bool(payload.get("sandboxed"))
    reason = str(payload.get("sandboxReason") or payload.get("error") or "unavailable")
    if not needs_install_guidance(
        mode=settings.bash_sandbox_mode,
        sandboxed=sandboxed,
        reason=reason,
    ):
        return None
    return user_status_message(reason=reason, mode=settings.bash_sandbox_mode)


def enrich_bash_result(
    payload: dict[str, Any],
    *,
    settings: Settings,
    support: SandboxSupport | None = None,
) -> dict[str, Any]:
    """Attach install guidance when a Bash result ran (or failed) without a sandbox."""

    sandboxed = bool(payload.get("sandboxed"))
    reason = str(payload.get("sandboxReason") or "")
    if support is not None and not reason:
        reason = support.reason
        payload["sandboxReason"] = reason
    if needs_install_guidance(
        mode=settings.bash_sandbox_mode,
        sandboxed=sandboxed,
        reason=reason,
    ):
        payload.update(install_guidance_payload(reason=reason))
    return payload
