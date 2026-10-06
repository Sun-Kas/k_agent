"""本地路径边界和输出预算的共享业务辅助函数。"""

from __future__ import annotations
from pathlib import Path
from typing import Any
from backend.tools.contracts import ToolExecutionContext, ToolOutcome, json_outcome


async def _workspace_root(ctx: ToolExecutionContext) -> Path:
    """工作区只来自本次物理执行上下文，不从进程配置猜测请求路径。"""
    if ctx.workspace is None:
        raise ValueError("Tool workspace is not bound")
    return ctx.workspace.resolve()


async def _resolve_workspace_path(
    ctx: ToolExecutionContext, raw_path: str, *, allow_outside: bool = False
) -> Path:
    """将模型传入的路径限制在工作区内，避免本地工具越权读写用户其它目录。"""
    root = await _workspace_root(ctx)
    candidate = Path(raw_path or ".").expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    # 必须先 resolve 再比较：它会展开 `..` 和符号链接，
    # 否则 `workspace/../../etc/passwd` 这类路径能绕过下面的包含检查。
    resolved = candidate.resolve()
    if allow_outside or ctx.permission_mode == "full_access":
        return resolved
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside workspace: {raw_path}") from exc
    return resolved


def _requests_host_access(ctx: ToolExecutionContext, payload: dict[str, Any]) -> bool:
    """Only approved escalations or full-access runs may cross the workspace."""

    return (
        ctx.permission_mode == "full_access"
        or payload.get("sandbox_permissions") == "require_escalated"
    )


async def _tool_limits(ctx: ToolExecutionContext) -> tuple[float, int]:
    """使用构建本轮 Binding 时冻结的预算。"""
    return ctx.output_limits.bash_timeout_seconds, ctx.output_limits.max_chars


def _json(payload: dict[str, Any]) -> ToolOutcome:
    """把工具输出对象序列化为紧凑 JSON 字符串。"""
    return json_outcome(payload)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """按最大字符数截断工具输出。"""
    # 截断标记要留在输出里：模型必须知道自己看到的是残缺内容，
    # 否则会基于半截文件或半截命令输出下结论。
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n\n[truncated: kept first {max_chars} chars]", True
