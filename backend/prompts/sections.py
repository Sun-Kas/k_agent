"""可复用的 prompt 区块，以及按指纹键控的进程内 section 缓存。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSection:
    """描述一个可渲染的 prompt 区块。"""
    name: str
    content: str


class PromptSectionCache:
    """稳定 prompt 区块的小进程内缓存。

    已连接 MCP 工具等动态区块应每请求重建；静态区块可复用到设置/记忆变更。
    """

    def __init__(self) -> None:
        """空缓存表；键为 `(section_name, fingerprint)`。"""
        self._cache: dict[tuple[str, str], str] = {}

    def get(self, name: str, fingerprint: str, compute: Callable[[], str]) -> str:
        """指纹命中则复用，否则调用 `compute` 并写入。"""
        key = (name, fingerprint)
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    def clear(self) -> None:
        """清空全部 section 缓存（配合 prompt lifecycle reset）。"""
        self._cache.clear()


SECTION_CACHE = PromptSectionCache()


def fingerprint_text(value: str) -> str:
    """计算文本内容指纹用于缓存失效。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def render_sections(sections: list[PromptSection]) -> str:
    """按顺序渲染多个 prompt 区块。"""
    return "\n\n".join(section.content.strip() for section in sections if section.content.strip())
