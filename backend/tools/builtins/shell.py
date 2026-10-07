"""shell 领域工具；类型化业务结果在此产生。"""

from __future__ import annotations
from backend.tools.contracts import ToolExecutionContext
import asyncio
from typing import Any
from backend.config import get_or_init_settings
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool
from backend.tools.contracts import ToolOutcome
import shlex
import re
from backend.sandbox import (
    SandboxUnavailable,
    build_child_env,
    enrich_bash_result,
    install_sandbox_runtime,
    plan_bash_invocation,
)
from backend.tools.builtins.common import (
    _workspace_root,
    _tool_limits,
    _json,
    _truncate,
)
from backend.tools.streaming import emit_tool_output


def _shell_subjects(arguments):
    """整句和每个分段都要过规则，避免链式命令绕过 deny。"""
    command = str(arguments.get("command") or "Bash")
    return (
        command,
        *(
            part.strip()
            for part in re.split(r"&&|\|\||;|\||\n", command)
            if part.strip()
        ),
    )


def _looks_like_interactive_auth(command: str) -> bool:
    """Recognize common user-driven auth flows without naming a specific CLI."""

    normalized = " ".join(command.lower().split())
    return bool(
        re.search(
            r"\b(?:auth|oauth)\s+(?:login|authorize|signin|sign-in)\b", normalized
        )
        or re.search(
            r"\b(?:login|signin|sign-in)\b[^;&|]*\b(?:device|oauth)\b", normalized
        )
        or re.search(r"\bdevice[-_ ]code\b", normalized)
    )


async def cc_bash(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """Run a time- and output-bounded shell command from the workspace root."""

    root = await _workspace_root(ctx)
    settings = await get_or_init_settings()
    command = str(payload.get("command") or "").strip()
    if not command:
        return _json({"ok": False, "error": "command is required"})
    default_timeout, max_chars = await _tool_limits(ctx)
    # Long but bounded jobs (for example, a Skill aggregating several APIs)
    # may declare their expected duration without requesting broader access.
    # The schema caps this value so a model cannot create an unbounded process.
    requested_mode = str(payload.get("execution_mode") or "auto")
    execution_mode = (
        "interactive"
        if requested_mode == "auto" and _looks_like_interactive_auth(command)
        else "foreground"
        if requested_mode == "auto"
        else requested_mode
    )
    timeout = float(
        payload.get(
            "timeout_seconds",
            max(default_timeout, 300.0)
            if execution_mode == "interactive"
            else default_timeout,
        )
    )
    try:
        invocation = plan_bash_invocation(
            command,
            workspace_root=root,
            settings=settings,
            network_access=ctx.network_access,
            full_access=(
                ctx.permission_mode == "full_access"
                or payload.get("sandbox_permissions") == "require_escalated"
            ),
        )
    except SandboxUnavailable as exc:
        return _json(
            enrich_bash_result(
                {
                    "ok": False,
                    "error": f"sandbox unavailable: {exc}",
                    "command": command,
                    "sandboxed": False,
                    "sandboxReason": str(exc),
                },
                settings=settings,
            )
        )
    # Env scrubbing is independent of the OS sandbox: a Seatbelt profile cannot
    # stop the child from reading whatever the parent put in its environ.
    child_env = build_child_env()
    if execution_mode == "interactive":
        # OAuth/device-code CLIs must print their URL instead of trying to open a
        # browser on the headless backend host. K_AGENT_INTERACTIVE is also a
        # generic capability signal that project CLIs may opt into later.
        child_env.update({"BROWSER": "echo", "CI": "1", "K_AGENT_INTERACTIVE": "1"})
    if invocation.argv is None:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=root,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *invocation.argv,
            cwd=root,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    captured = {"stdout": bytearray(), "stderr": bytearray()}

    async def consume(stream_name: str, reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        while chunk := await reader.read(4096):
            # Keep the final result bounded while still draining both pipes so a
            # verbose child cannot deadlock. Live output is forwarded immediately.
            remaining = max(0, max_chars * 4 - len(captured[stream_name]))
            captured[stream_name].extend(chunk[:remaining])
            (ctx.emit_output or emit_tool_output)(
                stream=stream_name,
                delta=chunk.decode(errors="replace"),
                executionMode=execution_mode,
            )

    stdout_task = asyncio.create_task(consume("stdout", process.stdout))
    stderr_task = asyncio.create_task(consume("stderr", process.stderr))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        await asyncio.gather(stdout_task, stderr_task)
    except TimeoutError:
        # kill 之后必须 wait，否则子进程留成僵尸；长时间运行的服务会逐渐堆积。
        process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return _json(
            enrich_bash_result(
                {
                    "ok": False,
                    "error": "command timed out",
                    "command": command,
                    "timeoutSeconds": timeout,
                    "sandboxed": invocation.sandboxed,
                    "sandboxReason": invocation.reason,
                },
                settings=settings,
            )
        )
    except asyncio.CancelledError:
        # Cancelling a task must also stop an OAuth CLI that may otherwise wait
        # indefinitely for a browser callback after its HTTP client disappeared.
        process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    stdout, stdout_truncated = _truncate(
        captured["stdout"].decode(errors="replace"), max_chars
    )
    stderr, stderr_truncated = _truncate(
        captured["stderr"].decode(errors="replace"), max_chars
    )
    return _json(
        enrich_bash_result(
            {
                "ok": process.returncode == 0,
                "command": command,
                "display": payload.get("description") or shlex.split(command)[0],
                "exitCode": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_truncated or stderr_truncated,
                "sandboxed": invocation.sandboxed,
                "sandboxReason": invocation.reason,
            },
            settings=settings,
        )
    )


async def cc_install_sandbox(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
    """Install srt only after the user has explicitly confirmed in chat."""

    settings = await get_or_init_settings()
    confirmed = payload.get("confirmed") is True
    result = await install_sandbox_runtime(
        confirmed=confirmed,
        sandbox_command=settings.bash_sandbox_command,
    )
    return _json(result)


BASH_TOOL = define_tool(
    name="Bash",
    description=(
        "Run a shell command in the workspace with a bounded timeout and streamed output. Use execution_mode for interactive commands and timeout_seconds for duration."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "description": {
                "type": "string",
                "description": "Explain why the command or requested resource is required.",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 300,
                "description": "Bounded wall-clock timeout for an inherently long command. Increasing it does not grant additional permissions.",
            },
            "execution_mode": {
                "type": "string",
                "enum": ["auto", "foreground", "interactive"],
                "default": "auto",
                "description": "Auto-detect OAuth/device-code login commands; use interactive explicitly for other commands that print a URL and wait for the user.",
            },
            "sandbox_permissions": {
                "type": "string",
                "enum": ["require_escalated"],
                "description": "Request HITL only for a structured out-of-sandbox resource or a concrete hostname outside the domain allowlist.",
            },
            "escalation_scope": {
                "type": "string",
                "enum": [
                    "outside_workspace_write",
                    "host_resource",
                    "network_destination",
                ],
                "description": "Required with require_escalated: the exact class of access requested.",
            },
            "escalation_resource": {
                "type": "string",
                "description": "Required with require_escalated: concrete outside path, host resource, or exact network hostname without scheme/path/port.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    execute=cc_bash,
    # Bash schema 本身不能证明命令只读；在引入可靠命令分类器前保守 retain。
    context_policy={"mode": "retain", "maxResultChars": 50_000},
    execution_policy=ToolExecutionPolicy("external", supports_live_output=True),
    permission_subjects=_shell_subjects,
)

INSTALLSANDBOX_TOOL = define_tool(
    name="InstallSandbox",
    description=(
        "Install Anthropic sandbox-runtime (srt) for Bash isolation. "
        "Call only after the user explicitly confirms installation in chat. "
        "Set confirmed=true; never install without confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "confirmed": {
                "type": "boolean",
                "description": "Must be true only after the user explicitly agrees to install.",
            }
        },
        "required": ["confirmed"],
        "additionalProperties": False,
    },
    execute=cc_install_sandbox,
    execution_policy=ToolExecutionPolicy("external", supports_live_output=False),
)

SHELL_TOOL_FACTORIES = (
    BASH_TOOL,
    INSTALLSANDBOX_TOOL,
)
