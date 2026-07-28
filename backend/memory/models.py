from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MemoryType(str, Enum):
    MANAGED = "managed"
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    AUTOMATED = "auto"
    TEAM = "team"


@dataclass(frozen=True)
class MemoryFile:
    path: Path
    content: str
    type: MemoryType
    raw_content: str | None = None
    includes: tuple[Path, ...] = ()
    globs: tuple[str, ...] = ()
    content_differs_from_disk: bool = False


@dataclass(frozen=True)
class ParsedMemory:
    content: str
    globs: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    content_differs_from_disk: bool = False


@dataclass
class MemoryLoadReport:
    files: list[MemoryFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [str(item.path) for item in self.files]

