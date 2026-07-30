"""Filesystem policy checks that keep memory loading within approved roots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from backend.memory.constants import ALLOWED_INCLUDE_EXTENSIONS


@dataclass(frozen=True)
class MemoryPolicy:
    """Feature flags and trust boundary used during memory discovery."""

    include_external: bool = False
    allow_user_memory: bool = True
    allow_project_memory: bool = True
    allow_local_memory: bool = True
    allow_auto_memory: bool = True
    allow_legacy_claude_paths: bool = True


def policy_from_env(*, include_external: bool = False) -> MemoryPolicy:
    """从环境变量构建 memory 读取策略。"""
    return MemoryPolicy(
        include_external=include_external or _truthy(os.getenv("K_AGENT_ALLOW_EXTERNAL_MEMORY_INCLUDES")),
        allow_user_memory=not _truthy(os.getenv("K_AGENT_DISABLE_USER_MEMORY") or os.getenv("CLAUDE_CODE_DISABLE_USER_MEMORY")),
        allow_project_memory=not _truthy(os.getenv("K_AGENT_DISABLE_PROJECT_MEMORY") or os.getenv("CLAUDE_CODE_DISABLE_PROJECT_MEMORY")),
        allow_local_memory=not _truthy(os.getenv("K_AGENT_DISABLE_LOCAL_MEMORY") or os.getenv("CLAUDE_CODE_DISABLE_LOCAL_MEMORY")),
        allow_auto_memory=not _truthy(os.getenv("K_AGENT_DISABLE_AUTO_MEMORY") or os.getenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY")),
        allow_legacy_claude_paths=not _truthy(os.getenv("K_AGENT_DISABLE_LEGACY_CLAUDE_MEMORY")),
    )


def can_read_memory_path(path: Path) -> tuple[bool, str | None]:
    """判断指定 memory 路径是否允许读取。"""
    if path.suffix.lower() not in ALLOWED_INCLUDE_EXTENSIONS:
        return False, f"unsupported extension: {path}"
    return True, None


def can_include_path(path: Path, base_dir: Path, policy: MemoryPolicy) -> tuple[bool, str | None]:
    """Check both file type and whether an include escapes its owning directory."""

    allowed, reason = can_read_memory_path(path)
    if not allowed:
        return False, reason
    if not policy.include_external and not is_relative_to(path, base_dir):
        return False, f"external include not approved: {path}"
    return True, None


def is_relative_to(path: Path, parent: Path) -> bool:
    """判断路径是否位于父目录之下。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def truthy(value: str | None) -> bool:
    """解析对外可调用的布尔字符串。"""
    return _truthy(value)


def _truthy(value: str | None) -> bool:
    """按常见字符串规则解析布尔值。"""
    return value is not None and value.lower() not in {"", "0", "false", "no", "off"}
