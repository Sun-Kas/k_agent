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
from backend.tools.local import ToolDefinition


async def _workspace_root() -> Path:
    settings = await get_or_init_settings()
    root = Path(settings.local_tool_workspace_root).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


async def _resolve_workspace_path(raw_path: str) -> Path:
    """将模型传入的路径限制在工作区内，避免本地工具越权读写用户其它目录。"""
    root = await _workspace_root()
    candidate = Path(raw_path or ".").expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside workspace: {raw_path}") from exc
    return resolved


async def _tool_limits() -> tuple[float, int]:
    settings = await get_or_init_settings()
    return settings.local_tool_bash_timeout_seconds, settings.local_tool_max_output_chars


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n\n[truncated: kept first {max_chars} chars]", True


async def cc_read(payload: dict[str, Any]) -> str:
    path = await _resolve_workspace_path(str(payload.get("file_path") or payload.get("path") or ""))
    if not path.exists():
        return _json({"ok": False, "error": "file not found", "path": str(path)})
    if path.is_dir():
        return _json({"ok": False, "error": "path is a directory", "path": str(path)})
    _, max_chars = await _tool_limits()
    content, truncated = _truncate(path.read_text(encoding="utf-8", errors="replace"), max_chars)
    return _json({"ok": True, "path": str(path), "content": content, "truncated": truncated})


async def cc_write(payload: dict[str, Any]) -> str:
    path = await _resolve_workspace_path(str(payload.get("file_path") or payload.get("path") or ""))
    content = str(payload.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _json({"ok": True, "path": str(path), "bytes": len(content.encode("utf-8"))})


async def cc_edit(payload: dict[str, Any]) -> str:
    path = await _resolve_workspace_path(str(payload.get("file_path") or payload.get("path") or ""))
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
    if occurrences > 1 and not replace_all:
        return _json({"ok": False, "error": "old_string is not unique", "occurrences": occurrences})
    updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    path.write_text(updated, encoding="utf-8")
    return _json({"ok": True, "path": str(path), "replacements": occurrences if replace_all else 1})


async def cc_glob(payload: dict[str, Any]) -> str:
    root = await _resolve_workspace_path(str(payload.get("path") or "."))
    pattern = str(payload.get("pattern") or "**/*")
    _, max_chars = await _tool_limits()
    matches = []
    for item in glob.iglob(str(root / pattern), recursive=True):
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
    root = await _resolve_workspace_path(str(payload.get("path") or "."))
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
    root = await _workspace_root()
    command = str(payload.get("command") or "").strip()
    if not command:
        return _json({"ok": False, "error": "command is required"})
    timeout, max_chars = await _tool_limits()
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return _json({"ok": False, "error": "command timed out", "command": command, "timeoutSeconds": timeout})
    stdout, stdout_truncated = _truncate(stdout_bytes.decode(errors="replace"), max_chars)
    stderr, stderr_truncated = _truncate(stderr_bytes.decode(errors="replace"), max_chars)
    return _json({
        "ok": process.returncode == 0,
        "command": command,
        "display": payload.get("description") or shlex.split(command)[0],
        "exitCode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
    })


async def cc_todo_write(payload: dict[str, Any]) -> str:
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
        description="Read a UTF-8 text file from the workspace.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"], "additionalProperties": False},
        execute=cc_read,
    ),
    ToolDefinition(
        name="Write",
        description="Write UTF-8 text to a workspace file, creating parent directories as needed.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"], "additionalProperties": False},
        execute=cc_write,
    ),
    ToolDefinition(
        name="Edit",
        description="Replace text in a workspace file. old_string must be unique unless replace_all is true.",
        parameters={"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}}, "required": ["file_path", "old_string", "new_string"], "additionalProperties": False},
        execute=cc_edit,
    ),
    ToolDefinition(
        name="Glob",
        description="Find workspace files by glob pattern, sorted by recent modification time.",
        parameters={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["pattern"], "additionalProperties": False},
        execute=cc_glob,
    ),
    ToolDefinition(
        name="Grep",
        description="Search workspace files with a regular expression.",
        parameters={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}, "include": {"type": "string", "default": "**/*"}}, "required": ["pattern"], "additionalProperties": False},
        execute=cc_grep,
    ),
    ToolDefinition(
        name="Bash",
        description="Run a shell command in the workspace with timeout and output truncation.",
        parameters={"type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string"}}, "required": ["command"], "additionalProperties": False},
        execute=cc_bash,
    ),
    ToolDefinition(
        name="TodoWrite",
        description="Create or update the agent's visible todo list for the current task.",
        parameters={"type": "object", "properties": {"todos": {"type": "array", "items": {"oneOf": [{"type": "object"}, {"type": "string"}]}}}, "required": ["todos"], "additionalProperties": False},
        execute=cc_todo_write,
    ),
]
