"""Protocol implemented by persistent storage adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class StorageBackend(Protocol):
    """Shared persistence contract for memories, sessions, and future data.

    Keys are logical names such as "sessions/<id>.json". File storage maps them
    to paths; Redis or SQL implementations can map them to keys or rows.
    """

    async def exists(self, key: str) -> bool:
        """判断指定 key 是否存在。"""
        ...

    async def read_text(self, key: str) -> str | None:
        """读取指定 key 的文本内容。"""
        ...

    async def write_text(self, key: str, content: str) -> None:
        """把文本写入指定 key。"""
        ...

    async def append_text(self, key: str, content: str) -> None:
        """以追加方式写入文本；一批 history record 必须在一次调用中提交。"""
        ...

    async def read_text_range(
        self, key: str, *, start_line: int = 0, limit: int | None = None
    ) -> list[str]:
        """按行读取追加日志；第一版允许全量读取，接口为后续分页保留边界。"""
        ...

    async def read_json(self, key: str) -> dict[str, Any] | list[Any] | None:
        """读取并解析指定 key 的 JSON 内容。"""
        ...

    async def write_json(self, key: str, payload: dict[str, Any] | list[Any]) -> None:
        """把对象序列化为 JSON 后写入指定 key。"""
        ...

    async def compare_and_swap_json(
        self,
        key: str,
        *,
        expected_revision: int,
        payload: dict[str, Any],
    ) -> bool:
        """按顶层 ``revision`` 做存储级 CAS，成功时原子替换整个 JSON。"""
        ...

    async def delete(self, key: str) -> None:
        """删除指定 key 对应的文件。"""
        ...

    async def list(self, prefix: str = "", pattern: str = "*") -> list[Path]:
        """列出指定前缀下匹配模式的文件。"""
        ...

    def resolve(self, key: str) -> Path:
        """把逻辑 key 解析到存储根目录下的安全路径。"""
        ...
