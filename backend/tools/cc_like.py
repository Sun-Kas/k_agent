"""工作区作用域的文件/搜索/Shell/任务列表工具（Claude Code 风格）。

所有路径经 `_resolve_workspace_path` 限制在 ContextVar 绑定的 workspace 内；
Bash 再交给 sandbox 规划是否走 `srt`。
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
    """读取工作区内文件的有界 UTF-8 表示。"""

    path = await _resolve_workspace_path(
        str(payload.get("file_path") or payload.get("path") or ""),
        allow_outside=_requests_host_access(payload),
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
    """按 glob 模式在工作区内查找文件。"""
    root = await _resolve_workspace_path(
        str(payload.get("path") or "."),
        allow_outside=_requests_host_access(payload),
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
    """按正则在工作区文件内容中搜索。"""
    root = await _resolve_workspace_path(
        str(payload.get("path") or "."),
        allow_outside=_requests_host_access(payload),
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


async def cc_bash(payload: dict[str, Any]) -> str:
    """Run a time- and output-bounded shell command from the workspace root."""

    root = await _workspace_root()
    settings = await get_or_init_settings()
    command = str(payload.get("command") or "").strip()
    if not command:
        return _json({"ok": False, "error": "command is required"})
    timeout, max_chars = await _tool_limits()
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
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError:
        # kill 之后必须 wait，否则子进程留成僵尸；长时间运行的服务会逐渐堆积。
        process.kill()
        await process.wait()
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
    stdout, stdout_truncated = _truncate(
        stdout_bytes.decode(errors="replace"), max_chars
    )
    stderr, stderr_truncated = _truncate(
        stderr_bytes.decode(errors="replace"), max_chars
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
        description="Read a UTF-8 text file. Paths outside the workspace require sandbox_permissions=require_escalated so the user can approve them.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["file_path"], "additionalProperties": False},
        execute=cc_read,
    ),
    ToolDefinition(
        name="Write",
        description="Write UTF-8 text. Paths outside the workspace require sandbox_permissions=require_escalated so the user can approve them.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["file_path", "content"], "additionalProperties": False},
        execute=cc_write,
    ),
    ToolDefinition(
        name="Edit",
        description="Replace text in a file. old_string must be unique unless replace_all is true. Outside-workspace paths require sandbox_permissions=require_escalated.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["file_path", "old_string", "new_string"], "additionalProperties": False},
        execute=cc_edit,
    ),
    ToolDefinition(
        name="Glob",
        description="Find files by glob pattern, sorted by recent modification time. Outside-workspace paths require sandbox_permissions=require_escalated.",
        parameters={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["pattern"], "additionalProperties": False},
        execute=cc_glob,
    ),
    ToolDefinition(
        name="Grep",
        description="Search files with a regular expression. Outside-workspace paths require sandbox_permissions=require_escalated.",
        parameters={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}, "include": {"type": "string", "default": "**/*"}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["pattern"], "additionalProperties": False},
        execute=cc_grep,
    ),
    ToolDefinition(
        name="Bash",
        description=(
            "Run a shell command in the workspace with timeout and output truncation. "
            "Commands prefer an OS sandbox (srt). If the sandbox is unavailable, the "
            "result includes userMessage/installGuidance — relay that full message to "
            "the user in Chinese (manual install vs confirm-then-InstallSandbox; "
            "Windows native unsupported / WSL2). Only call InstallSandbox after they "
            "explicitly confirm. In default permission mode, retry a command that "
            "needs host access with sandbox_permissions=require_escalated so HITL can "
            "ask the user. Full-access runs execute without the OS sandbox."
        ),
        parameters={"type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string"}, "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]}}, "required": ["command"], "additionalProperties": False},
        execute=cc_bash,
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
    ),
]
