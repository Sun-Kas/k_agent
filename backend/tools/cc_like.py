"""文件/搜索/Shell/任务列表工具（Claude Code 风格）。

只读文件工具可访问本机路径；写工具默认限制在 ContextVar 绑定的 workspace，
越界写入需要审批。Bash 再交给 sandbox 规划是否走 `srt`。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from backend.config import get_or_init_settings
from backend.sandbox import (
    SandboxUnavailable,
    build_child_env,
    enrich_bash_result,
    install_sandbox_runtime,
    plan_bash_invocation,
)
from backend.tools.local import ToolDefinition
from backend.tools.streaming import emit_tool_output
from backend.tools.workspace import (
    current_tool_network_access,
    current_tool_permission_mode,
    current_tool_workspace,
)


async def _workspace_root() -> Path:
    """当前 ContextVar 绑定的工作区；未绑定时回落 Settings 配置根。"""
    scoped = current_tool_workspace()
    if scoped is not None:
        return scoped
    settings = await get_or_init_settings()
    root = Path(settings.local_tool_workspace_root).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


async def _resolve_workspace_path(raw_path: str, *, allow_outside: bool = False) -> Path:
    """将模型传入的路径限制在工作区内，避免本地工具越权读写用户其它目录。"""
    root = await _workspace_root()
    candidate = Path(raw_path or ".").expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    # 必须先 resolve 再比较：它会展开 `..` 和符号链接，
    # 否则 `workspace/../../etc/passwd` 这类路径能绕过下面的包含检查。
    resolved = candidate.resolve()
    if allow_outside or current_tool_permission_mode() == "full_access":
        return resolved
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside workspace: {raw_path}") from exc
    return resolved


def _requests_host_access(payload: dict[str, Any]) -> bool:
    """Only approved escalations or full-access runs may cross the workspace."""

    return (
        current_tool_permission_mode() == "full_access"
        or payload.get("sandbox_permissions") == "require_escalated"
    )


async def _tool_limits() -> tuple[float, int]:
    """读取工具执行超时和输出长度限制。"""
    settings = await get_or_init_settings()
    return settings.local_tool_bash_timeout_seconds, settings.local_tool_max_output_chars


def _json(payload: dict[str, Any]) -> str:
    """把工具输出对象序列化为紧凑 JSON 字符串。"""
    return json.dumps(payload, ensure_ascii=False)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """按最大字符数截断工具输出。"""
    # 截断标记要留在输出里：模型必须知道自己看到的是残缺内容，
    # 否则会基于半截文件或半截命令输出下结论。
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n\n[truncated: kept first {max_chars} chars]", True


async def cc_read(payload: dict[str, Any]) -> str:
    """读取任意本机普通文件的有界 UTF-8 表示。"""

    path = await _resolve_workspace_path(
        str(payload.get("file_path") or payload.get("path") or ""),
        # Read-only filesystem access is not a mutation boundary. Keeping this
        # separate from Write/Edit prevents harmless project reads from HITL.
        allow_outside=True,
    )
    if not path.exists():
        return _json({"ok": False, "error": "file not found", "path": str(path)})
    if path.is_dir():
        return _json({"ok": False, "error": "path is a directory", "path": str(path)})
    _, max_chars = await _tool_limits()
    content, truncated = _truncate(path.read_text(encoding="utf-8", errors="replace"), max_chars)
    return _json({"ok": True, "path": str(path), "content": content, "truncated": truncated})


async def cc_write(payload: dict[str, Any]) -> str:
    """Write a file after resolving its target inside the workspace boundary."""

    path = await _resolve_workspace_path(
        str(payload.get("file_path") or payload.get("path") or ""),
        allow_outside=_requests_host_access(payload),
    )
    content = str(payload.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _json({"ok": True, "path": str(path), "bytes": len(content.encode("utf-8"))})


async def cc_edit(payload: dict[str, Any]) -> str:
    """Apply an exact replacement and reject ambiguous matches by default."""

    path = await _resolve_workspace_path(
        str(payload.get("file_path") or payload.get("path") or ""),
        allow_outside=_requests_host_access(payload),
    )
    old_string = str(payload.get("old_string") or payload.get("oldString") or "")
    new_string = str(payload.get("new_string") or payload.get("newString") or "")
    replace_all = bool(payload.get("replace_all") or payload.get("replaceAll") or False)
    if not old_string:
        return _json({"ok": False, "error": "old_string is required"})
    if not path.exists() or path.is_dir():
        return _json({"ok": False, "error": "file not found", "path": str(path)})
    content = path.read_text(encoding="utf-8", errors="replace")
    occurrences = content.count(old_string)
    if occurrences == 0:
        return _json({"ok": False, "error": "old_string not found", "path": str(path)})
    # 匹配到多处却没显式要求全替换时拒绝执行：模型多半只想改其中一处，
    # 默默改第一处会造成静默的错误编辑，报错让它补充上下文重试更安全。
    if occurrences > 1 and not replace_all:
        return _json({"ok": False, "error": "old_string is not unique", "occurrences": occurrences})
    updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    path.write_text(updated, encoding="utf-8")
    return _json({"ok": True, "path": str(path), "replacements": occurrences if replace_all else 1})


async def cc_glob(payload: dict[str, Any]) -> str:
    """按 glob 模式在指定本机目录中查找文件。"""
    root = await _resolve_workspace_path(
        str(payload.get("path") or "."),
        allow_outside=True,
    )
    pattern = str(payload.get("pattern") or "**/*")
    _, max_chars = await _tool_limits()
    matches = []
    for item in glob.iglob(str(root / pattern), recursive=True):
        # pattern 未经工作区校验，`../` 或符号链接都可能把匹配结果指到工作区外，
        # 所以逐条结果再确认一次包含关系。
        path = Path(item).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        matches.append(str(path))
    matches.sort(key=lambda item: os.path.getmtime(item) if os.path.exists(item) else 0, reverse=True)
    content, truncated = _truncate("\n".join(matches), max_chars)
    return _json({"ok": True, "root": str(root), "matches": content.splitlines() if content else [], "truncated": truncated})


async def cc_grep(payload: dict[str, Any]) -> str:
    """按正则在指定本机目录的文件内容中搜索。"""
    root = await _resolve_workspace_path(
        str(payload.get("path") or "."),
        allow_outside=True,
    )
    pattern = str(payload.get("pattern") or "")
    include = str(payload.get("include") or "**/*")
    if not pattern:
        return _json({"ok": False, "error": "pattern is required"})
    regex = re.compile(pattern)
    _, max_chars = await _tool_limits()
    lines: list[str] = []
    for item in glob.iglob(str(root / include), recursive=True):
        path = Path(item)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                lines.append(f"{path}:{line_no}:{line}")
    content, truncated = _truncate("\n".join(lines), max_chars)
    return _json({"ok": True, "root": str(root), "matches": content.splitlines() if content else [], "truncated": truncated})


def _looks_like_interactive_auth(command: str) -> bool:
    """Recognize common user-driven auth flows without naming a specific CLI."""

    normalized = " ".join(command.lower().split())
    return bool(
        re.search(r"\b(?:auth|oauth)\s+(?:login|authorize|signin|sign-in)\b", normalized)
        or re.search(r"\b(?:login|signin|sign-in)\b[^;&|]*\b(?:device|oauth)\b", normalized)
        or re.search(r"\bdevice[-_ ]code\b", normalized)
    )


async def cc_bash(payload: dict[str, Any]) -> str:
    """Run a time- and output-bounded shell command from the workspace root."""

    root = await _workspace_root()
    settings = await get_or_init_settings()
    command = str(payload.get("command") or "").strip()
    if not command:
        return _json({"ok": False, "error": "command is required"})
    default_timeout, max_chars = await _tool_limits()
    # Long but bounded jobs (for example, a Skill aggregating several APIs)
    # may declare their expected duration without requesting broader access.
    # The schema caps this value so a model cannot create an unbounded process.
    requested_mode = str(payload.get("execution_mode") or "auto")
    execution_mode = (
        "interactive"
        if requested_mode == "auto" and _looks_like_interactive_auth(command)
        else "foreground" if requested_mode == "auto" else requested_mode
    )
    timeout = float(payload.get(
        "timeout_seconds",
        max(default_timeout, 300.0) if execution_mode == "interactive" else default_timeout,
    ))
    try:
        invocation = plan_bash_invocation(
            command,
            workspace_root=root,
            settings=settings,
            network_access=current_tool_network_access(),
            full_access=(
                current_tool_permission_mode() == "full_access"
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
            emit_tool_output(
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
    stdout, stdout_truncated = _truncate(captured["stdout"].decode(errors="replace"), max_chars)
    stderr, stderr_truncated = _truncate(captured["stderr"].decode(errors="replace"), max_chars)
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


async def cc_install_sandbox(payload: dict[str, Any]) -> str:
    """Install srt only after the user has explicitly confirmed in chat."""

    settings = await get_or_init_settings()
    confirmed = payload.get("confirmed") is True
    result = await install_sandbox_runtime(
        confirmed=confirmed,
        sandbox_command=settings.bash_sandbox_command,
    )
    return _json(result)


async def cc_todo_write(payload: dict[str, Any]) -> str:
    """记录并返回模型提交的任务列表。"""
    todos = payload.get("todos")
    if not isinstance(todos, list):
        return _json({"ok": False, "error": "todos must be a list"})
    normalized = []
    for index, item in enumerate(todos, start=1):
        if isinstance(item, str):
            normalized.append({"id": str(index), "content": item, "status": "pending"})
        elif isinstance(item, dict):
            normalized.append({
                "id": str(item.get("id") or index),
                "content": str(item.get("content") or ""),
                "status": str(item.get("status") or "pending"),
            })
    return _json({"ok": True, "todos": normalized})


CC_LIKE_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="Read",
        description="Read a UTF-8 text file from any local path. Read-only access does not require permission escalation.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"], "additionalProperties": False},
        execute=cc_read,
        context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    ),
    ToolDefinition(
        name="Write",
        description="Write UTF-8 text. Paths outside the workspace require sandbox_permissions=require_escalated so the user can approve them.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["file_path", "content"], "additionalProperties": False},
        execute=cc_write,
        context_policy={"mode": "receipt", "maxResultChars": 12_000},
    ),
    ToolDefinition(
        name="Edit",
        description="Replace text in a file. old_string must be unique unless replace_all is true. Outside-workspace paths require sandbox_permissions=require_escalated.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["file_path", "old_string", "new_string"], "additionalProperties": False},
        execute=cc_edit,
        context_policy={"mode": "receipt", "maxResultChars": 12_000},
    ),
    ToolDefinition(
        name="Glob",
        description="Find files under any local directory by glob pattern, sorted by recent modification time. Read-only access does not require permission escalation.",
        parameters={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["pattern"], "additionalProperties": False},
        execute=cc_glob,
        context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    ),
    ToolDefinition(
        name="Grep",
        description="Search files under any local directory with a regular expression. Read-only access does not require permission escalation.",
        parameters={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}, "include": {"type": "string", "default": "**/*"}}, "required": ["pattern"], "additionalProperties": False},
        execute=cc_grep,
        context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    ),
    ToolDefinition(
        name="Bash",
        description=(
            "Run a shell command in the workspace with timeout and output truncation. "
            "Commands prefer an OS sandbox (srt). If the sandbox is unavailable, the "
            "result includes userMessage/installGuidance — relay that full message to "
            "the user in Chinese (manual install vs confirm-then-InstallSandbox; "
            "Windows native unsupported / WSL2). Only call InstallSandbox after they "
            "explicitly confirm. Set sandbox_permissions=require_escalated ONLY when "
            "the user's task necessarily requires this command to (1) write, modify, "
            "or delete a path outside the session workspace; (2) access a host device, "
            "local socket, GUI, process, credential store, or system service blocked by "
            "the default sandbox; or (3) connect to a concrete hostname outside the "
            "configured sandbox domain allowlist. Also set escalation_scope and "
            "escalation_resource. A network escalation resource must be only the exact "
            "hostname, not a URL. Timeouts, DNS/HTTP errors, connection resets, truncated "
            "responses, and rate limits do not trigger escalation. For an inherently long "
            "but bounded command, set timeout_seconds "
            "instead of requesting escalation; this changes duration, not permissions. "
            "For OAuth, device-code login, or another command that waits for a user "
            "in an external web page, set execution_mode=interactive. Interactive mode "
            "streams stdout while the process is running and prevents the backend from "
            "opening a browser; do not pipe that command through head or tail. "
            "Full-access runs execute without the OS sandbox."
        ),
        parameters={"type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string", "description": "Explain why the command or requested resource is required."}, "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300, "description": "Bounded wall-clock timeout for an inherently long command. Increasing it does not grant additional permissions."}, "execution_mode": {"type": "string", "enum": ["auto", "foreground", "interactive"], "default": "auto", "description": "Auto-detect OAuth/device-code login commands; use interactive explicitly for other commands that print a URL and wait for the user."}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"], "description": "Request HITL only for a structured out-of-sandbox resource or a concrete hostname outside the domain allowlist."}, "escalation_scope": {"type": "string", "enum": ["outside_workspace_write", "host_resource", "network_destination"], "description": "Required with require_escalated: the exact class of access requested."}, "escalation_resource": {"type": "string", "description": "Required with require_escalated: concrete outside path, host resource, or exact network hostname without scheme/path/port."}}, "required": ["command"], "additionalProperties": False},
        execute=cc_bash,
        # Bash schema 本身不能证明命令只读；在引入可靠命令分类器前保守 retain。
        context_policy={"mode": "retain", "maxResultChars": 50_000},
    ),
    ToolDefinition(
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
    ),
    ToolDefinition(
        name="TodoWrite",
        description="Create or update the agent's visible todo list for the current task.",
        parameters={"type": "object", "properties": {"todos": {"type": "array", "items": {"oneOf": [{"type": "object"}, {"type": "string"}]}}}, "required": ["todos"], "additionalProperties": False},
        execute=cc_todo_write,
        context_policy={"mode": "receipt", "maxResultChars": 12_000},
    ),
]
