"""Data models describing discovered, parsed, and loaded memory content."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MemoryType(str, Enum):
    """枚举 memory 文件来源类型。"""
    MANAGED = "managed"
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    AUTOMATED = "auto"
    TEAM = "team"


@dataclass(frozen=True)
class MemoryFile:
    """描述单个 memory 文件内容和元数据。"""
    path: Path
    content: str
    type: MemoryType
    raw_content: str | None = None
    includes: tuple[Path, ...] = ()
    globs: tuple[str, ...] = ()
    content_differs_from_disk: bool = False


@dataclass(frozen=True)
class ParsedMemory:
    """描述解析后的 memory 正文、include 和 globs。"""
    content: str
    globs: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    content_differs_from_disk: bool = False


@dataclass
class MemoryLoadReport:
    """描述 memory 加载结果和跳过原因。"""
    files: list[MemoryFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        """返回报告中已加载 memory 文件路径。"""
        return [str(item.path) for item in self.files]
