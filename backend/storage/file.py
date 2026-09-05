"""基于本地文件系统的异步存储适配器（逻辑 key → 路径）。

pipeline：Access Layer / Backend 经 `create_storage` 取得本实现；
会话、配置与部分记忆读写落在该适配器上，调用方不感知具体目录布局。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any


class FileStorage:
    """把逻辑 string key 映射到 `base_dir` 下文件；可替换为 DB 等同源接口实现。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        """设定存储根目录；后续相对 key 均相对该根解析。"""
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

    async def append_text(self, key: str, content: str) -> None:
        """使用 O_APPEND 一次写入完整批次。"""

        await asyncio.to_thread(append_text_durable, self.resolve(key), content)

    async def read_text_range(
        self, key: str, *, start_line: int = 0, limit: int | None = None
    ) -> list[str]:
        if start_line < 0 or (limit is not None and limit < 0):
            return []
        content = await self.read_text(key)
        if content is None:
            return []
        lines = content.splitlines()
        return lines[start_line:] if limit is None else lines[start_line : start_line + limit]

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


def append_text_durable(path: Path, content: str) -> None:
    """单次追加并 fsync，避免 history 批次被截成半条 JSON。"""

    if not content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        data = content.encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write a config document without leaving a truncated file behind."""

    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
