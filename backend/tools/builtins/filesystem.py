"""filesystem 领域工具；类型化业务结果在此产生。"""

from __future__ import annotations
from backend.tools.contracts import ToolExecutionContext
import json
from pathlib import Path
from typing import Any
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool
from backend.tools.contracts import ToolOutcome
import glob
import os
import re
from backend.tools.builtins.common import (
    _requests_host_access,
    _resolve_workspace_path,
    _tool_limits,
    _json,
    _truncate,
)
from backend.tools.file_hunk import hunks_for_edit, unified_hunks


async def cc_read(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """读取任意本机普通文件的有界 UTF-8 表示。"""

    path = await _resolve_workspace_path(
        ctx,
        str(payload.get("file_path") or payload.get("path") or ""),
        # Read-only filesystem access is not a mutation boundary. Keeping this
        # separate from Write/Edit prevents harmless project reads from HITL.
        allow_outside=True,
    )
    if not path.exists():
        return _json({"ok": False, "error": "file not found", "path": str(path)})
    if path.is_dir():
        return _json({"ok": False, "error": "path is a directory", "path": str(path)})
    _, max_chars = await _tool_limits(ctx)
    content, truncated = _truncate(
        path.read_text(encoding="utf-8", errors="replace"), max_chars
    )
    return _json(
        {"ok": True, "path": str(path), "content": content, "truncated": truncated}
    )


async def cc_write(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """Write a file after resolving its target inside the workspace boundary."""

    path = await _resolve_workspace_path(
        ctx,
        str(payload.get("file_path") or payload.get("path") or ""),
        allow_outside=_requests_host_access(ctx, payload),
    )
    content = str(payload.get("content") or "")
    previous = path.read_text(encoding="utf-8", errors="replace") if path.exists() and path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _json(
        {
            "ok": True,
            "path": str(path),
            "bytes": len(content.encode("utf-8")),
            "hunks": unified_hunks(previous, content),
        }
    )


async def cc_edit(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """Apply an exact replacement and reject ambiguous matches by default."""

    path = await _resolve_workspace_path(
        ctx,
        str(payload.get("file_path") or payload.get("path") or ""),
        allow_outside=_requests_host_access(ctx, payload),
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
        return _json(
            {
                "ok": False,
                "error": "old_string is not unique",
                "occurrences": occurrences,
            }
        )
    updated = (
        content.replace(old_string, new_string)
        if replace_all
        else content.replace(old_string, new_string, 1)
    )
    path.write_text(updated, encoding="utf-8")
    return _json(
        {
            "ok": True,
            "path": str(path),
            "replacements": occurrences if replace_all else 1,
            "hunks": hunks_for_edit(
                content, old_string, new_string, replace_all=replace_all
            ),
        }
    )


async def cc_glob(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """按 glob 模式在指定本机目录中查找文件。"""
    root = await _resolve_workspace_path(
        ctx,
        str(payload.get("path") or "."),
        allow_outside=True,
    )
    pattern = str(payload.get("pattern") or "**/*")
    _, max_chars = await _tool_limits(ctx)
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
    matches.sort(
        key=lambda item: os.path.getmtime(item) if os.path.exists(item) else 0,
        reverse=True,
    )
    content, truncated = _truncate("\n".join(matches), max_chars)
    return _json(
        {
            "ok": True,
            "root": str(root),
            "matches": content.splitlines() if content else [],
            "truncated": truncated,
        }
    )


async def cc_grep(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """按正则在指定本机目录的文件内容中搜索。"""
    root = await _resolve_workspace_path(
        ctx,
        str(payload.get("path") or "."),
        allow_outside=True,
    )
    pattern = str(payload.get("pattern") or "")
    include = str(payload.get("include") or "**/*")
    if not pattern:
        return _json({"ok": False, "error": "pattern is required"})
    regex = re.compile(pattern)
    _, max_chars = await _tool_limits(ctx)
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
    return _json(
        {
            "ok": True,
            "root": str(root),
            "matches": content.splitlines() if content else [],
            "truncated": truncated,
        }
    )


async def cc_ls(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """列出任意本机目录内容；只读目录遍历无需提升写权限。"""
    path = await _resolve_workspace_path(
        ctx, str(payload.get("path") or "."), allow_outside=True
    )
    if not path.exists():
        return _json({"ok": False, "error": "path not found", "path": str(path)})
    if not path.is_dir():
        return _json(
            {"ok": False, "error": "path is not a directory", "path": str(path)}
        )

    max_entries = int(payload.get("max_entries") or payload.get("maxEntries") or 200)
    show_hidden = bool(payload.get("show_hidden") or payload.get("showHidden") or False)
    entries = []
    for child in sorted(
        path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
    ):
        if not show_hidden and child.name.startswith("."):
            continue
        stat = child.stat()
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "directory" if child.is_dir() else "file",
                "size": stat.st_size,
            }
        )
        if len(entries) >= max_entries:
            break
    return _json(
        {
            "ok": True,
            "path": str(path),
            "entries": entries,
            "truncated": len(entries) >= max_entries,
        }
    )


async def cc_notebook_edit(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
    """编辑 Jupyter notebook 单元格，只处理 ipynb JSON，不执行其中代码。"""
    path = await _resolve_workspace_path(
        ctx,
        str(payload.get("file_path") or payload.get("path") or ""),
        allow_outside=_requests_host_access(ctx, payload),
    )
    cell_index = int(
        payload.get("cell_index")
        if payload.get("cell_index") is not None
        else payload.get("cellIndex", -1)
    )
    source = payload.get("source")
    cell_type = str(payload.get("cell_type") or payload.get("cellType") or "code")
    edit_mode = str(payload.get("mode") or "replace")
    if cell_index < 0:
        return _json({"ok": False, "error": "cell_index is required"})
    if source is None and edit_mode != "delete":
        return _json({"ok": False, "error": "source is required"})
    # 限定后缀，避免这个专用编辑器被当成通用写文件工具绕过 Write 的权限规则。
    if path.suffix != ".ipynb":
        return _json({"ok": False, "error": "NotebookEdit only supports .ipynb files"})

    notebook = json.loads(path.read_text(encoding="utf-8"))
    cells = notebook.setdefault("cells", [])
    if edit_mode == "insert":
        if cell_index > len(cells):
            return _json({"ok": False, "error": "cell_index is out of range"})
        cells.insert(cell_index, _notebook_cell(cell_type, str(source)))
    elif edit_mode == "delete":
        if cell_index >= len(cells):
            return _json({"ok": False, "error": "cell_index is out of range"})
        del cells[cell_index]
    else:
        if cell_index >= len(cells):
            return _json({"ok": False, "error": "cell_index is out of range"})
        cells[cell_index] = {
            **cells[cell_index],
            **_notebook_cell(cell_type, str(source)),
        }
    # ipynb 是 JSON 文档，保持缩进可以减少后续人工 diff 的阅读成本。
    path.write_text(
        json.dumps(notebook, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return _json(
        {"ok": True, "path": str(path), "cellIndex": cell_index, "mode": edit_mode}
    )


def _notebook_cell(cell_type: str, source: str) -> dict[str, Any]:
    """按 Jupyter notebook 规范生成最小单元格结构。"""
    lines = source.splitlines(keepends=True)
    normalized = lines if lines else [""]
    if cell_type == "markdown":
        return {"cell_type": "markdown", "metadata": {}, "source": normalized}
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": normalized,
    }


READ_TOOL = define_tool(
    name="Read",
    description="Read a UTF-8 text file from any local path. Read-only access does not require permission escalation.",
    parameters={
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
        "additionalProperties": False,
    },
    execute=cc_read,
    context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
    permission_subjects=lambda args: (
        str(args.get("file_path") or args.get("path") or "Read"),
    ),
)

WRITE_TOOL = define_tool(
    name="Write",
    description="Write UTF-8 text. Paths outside the workspace require sandbox_permissions=require_escalated so the user can approve them.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
            "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]},
        },
        "required": ["file_path", "content"],
        "additionalProperties": False,
    },
    execute=cc_write,
    context_policy={"mode": "receipt", "maxResultChars": 12_000},
    execution_policy=ToolExecutionPolicy("write", supports_live_output=False),
    permission_subjects=lambda args: (
        str(args.get("file_path") or args.get("path") or "Write"),
    ),
)

EDIT_TOOL = define_tool(
    name="Edit",
    description="Replace text in a file. old_string must be unique unless replace_all is true. Outside-workspace paths require sandbox_permissions=require_escalated.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
            "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]},
        },
        "required": ["file_path", "old_string", "new_string"],
        "additionalProperties": False,
    },
    execute=cc_edit,
    context_policy={"mode": "receipt", "maxResultChars": 12_000},
    execution_policy=ToolExecutionPolicy("write", supports_live_output=False),
    permission_subjects=lambda args: (
        str(args.get("file_path") or args.get("path") or "Edit"),
    ),
)

GLOB_TOOL = define_tool(
    name="Glob",
    description="Find files under any local directory by glob pattern, sorted by recent modification time. Read-only access does not require permission escalation.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    execute=cc_glob,
    context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
    permission_subjects=lambda args: (
        str(args.get("file_path") or args.get("path") or "Glob"),
    ),
)

GREP_TOOL = define_tool(
    name="Grep",
    description="Search files under any local directory with a regular expression. Read-only access does not require permission escalation.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "include": {"type": "string", "default": "**/*"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    execute=cc_grep,
    context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
    permission_subjects=lambda args: (
        str(args.get("file_path") or args.get("path") or "Grep"),
    ),
)

LS_TOOL = define_tool(
    name="LS",
    description="List files and directories under any local directory. Read-only access does not require permission escalation.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "max_entries": {"type": "integer", "default": 200},
            "show_hidden": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    execute=cc_ls,
    context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
    permission_subjects=lambda args: (
        str(args.get("file_path") or args.get("path") or "LS"),
    ),
)

NOTEBOOKEDIT_TOOL = define_tool(
    name="NotebookEdit",
    description="Insert, replace, or delete a cell in a Jupyter .ipynb notebook. Editing outside the workspace requires sandbox_permissions=require_escalated.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "cell_index": {"type": "integer"},
            "source": {"type": "string"},
            "cell_type": {"type": "string", "default": "code"},
            "mode": {"type": "string", "default": "replace"},
            "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]},
        },
        "required": ["file_path", "cell_index"],
        "additionalProperties": False,
    },
    execute=cc_notebook_edit,
    context_policy={"mode": "receipt", "maxResultChars": 12_000},
    execution_policy=ToolExecutionPolicy("write", supports_live_output=False),
    permission_subjects=lambda args: (
        str(args.get("file_path") or args.get("path") or "NotebookEdit"),
    ),
)

FILESYSTEM_TOOL_FACTORIES = (
    READ_TOOL,
    WRITE_TOOL,
    EDIT_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    LS_TOOL,
    NOTEBOOKEDIT_TOOL,
)
