"""Filesystem implementation of the asynchronous storage interface."""

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
        """初始化对象依赖和内部状态。"""
        self.base_dir = Path(base_dir or ".").expanduser().resolve()

    def resolve(self, key: str) -> Path:
        """把逻辑 key 解析到存储根目录下的安全路径。"""
        path = Path(key).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (self.base_dir / path).resolve()

    async def exists(self, key: str) -> bool:
        """判断指定 key 是否存在。"""
        return await asyncio.to_thread(self.resolve(key).exists)

    def exists_sync(self, key: str) -> bool:
        """同步判断指定 key 是否存在。"""
        return self.resolve(key).exists()

    async def read_text(self, key: str) -> str | None:
        """读取指定 key 的文本内容。"""
        path = self.resolve(key)
        if not await asyncio.to_thread(path.is_file):
            return None
        return await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")

    def read_text_sync(self, key: str) -> str | None:
        """同步读取指定 key 的文本内容。"""
        path = self.resolve(key)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    async def write_text(self, key: str, content: str) -> None:
        """把文本写入指定 key。"""
        path = self.resolve(key)
        await asyncio.to_thread(write_text_atomic, path, content)

    async def read_json(self, key: str) -> dict[str, Any] | list[Any] | None:
        """读取并解析指定 key 的 JSON 内容。"""
        content = await self.read_text(key)
        if content is None:
            return None
        return json.loads(content)

    async def write_json(self, key: str, payload: dict[str, Any] | list[Any]) -> None:
        """把对象序列化为 JSON 后写入指定 key。"""
        await self.write_text(key, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    async def delete(self, key: str) -> None:
        """删除指定 key 对应的文件。"""
        path = self.resolve(key)
        if await asyncio.to_thread(path.exists):
            await asyncio.to_thread(path.unlink)

    async def list(self, prefix: str = "", pattern: str = "*") -> list[Path]:
        """列出指定前缀下匹配模式的文件。"""
        root = self.resolve(prefix)
        if not await asyncio.to_thread(root.exists):
            return []
        paths = await asyncio.to_thread(lambda: sorted(path.resolve() for path in root.rglob(pattern) if path.is_file()))
        return paths

    def list_sync(self, prefix: str = "", pattern: str = "*") -> list[Path]:
        """同步列出指定前缀下匹配模式的文件。"""
        root = self.resolve(prefix)
        if not root.exists():
            return []
        return sorted(path.resolve() for path in root.rglob(pattern) if path.is_file())


def write_text_atomic(path: Path, content: str) -> None:
    """通过临时文件和 replace 原子写入文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write a config document without leaving a truncated file behind."""

    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
