from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any


class FileStorage:
    """Filesystem-backed storage.

    The public methods use logical string keys so callers do not depend on a
    concrete filesystem layout. Database-backed implementations can keep the
    same contract and reinterpret keys as document IDs or namespaces.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir or ".").expanduser().resolve()

    def resolve(self, key: str) -> Path:
        path = Path(key).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (self.base_dir / path).resolve()

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self.resolve(key).exists)

    def exists_sync(self, key: str) -> bool:
        return self.resolve(key).exists()

    async def read_text(self, key: str) -> str | None:
        path = self.resolve(key)
        if not await asyncio.to_thread(path.is_file):
            return None
        return await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")

    def read_text_sync(self, key: str) -> str | None:
        path = self.resolve(key)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    async def write_text(self, key: str, content: str) -> None:
        path = self.resolve(key)
        await asyncio.to_thread(_atomic_write_text, path, content)

    async def read_json(self, key: str) -> dict[str, Any] | list[Any] | None:
        content = await self.read_text(key)
        if content is None:
            return None
        return json.loads(content)

    async def write_json(self, key: str, payload: dict[str, Any] | list[Any]) -> None:
        await self.write_text(key, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    async def delete(self, key: str) -> None:
        path = self.resolve(key)
        if await asyncio.to_thread(path.exists):
            await asyncio.to_thread(path.unlink)

    async def list(self, prefix: str = "", pattern: str = "*") -> list[Path]:
        root = self.resolve(prefix)
        if not await asyncio.to_thread(root.exists):
            return []
        paths = await asyncio.to_thread(lambda: sorted(path.resolve() for path in root.rglob(pattern) if path.is_file()))
        return paths

    def list_sync(self, prefix: str = "", pattern: str = "*") -> list[Path]:
        root = self.resolve(prefix)
        if not root.exists():
            return []
        return sorted(path.resolve() for path in root.rglob(pattern) if path.is_file())


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)
