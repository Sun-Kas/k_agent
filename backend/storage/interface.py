from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class StorageBackend(Protocol):
    """Shared persistence contract for memories, sessions, and future data.

    Keys are logical names such as "sessions/<id>.json". File storage maps them
    to paths; Redis or SQL implementations can map them to keys or rows.
    """

    async def exists(self, key: str) -> bool:
        ...

    async def read_text(self, key: str) -> str | None:
        ...

    async def write_text(self, key: str, content: str) -> None:
        ...

    async def read_json(self, key: str) -> dict[str, Any] | list[Any] | None:
        ...

    async def write_json(self, key: str, payload: dict[str, Any] | list[Any]) -> None:
        ...

    async def delete(self, key: str) -> None:
        ...

    async def list(self, prefix: str = "", pattern: str = "*") -> list[Path]:
        ...

    def resolve(self, key: str) -> Path:
        ...
