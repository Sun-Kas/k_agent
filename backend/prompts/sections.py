"""Reusable prompt sections and a fingerprint-keyed section cache."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSection:
    """描述一个可渲染的 prompt 区块。"""
    name: str
    content: str
    cacheable: bool = True


class PromptSectionCache:
    """Small in-process cache for stable prompt sections.

    Dynamic sections such as connected MCP tools should be rebuilt every request;
    static sections can be reused until settings or memory state changes.
    """

    def __init__(self) -> None:
        """初始化对象依赖和内部状态。"""
        self._cache: dict[tuple[str, str], str] = {}

    def get(self, name: str, fingerprint: str, compute: Callable[[], str]) -> str:
        """读取或创建当前对象管理的条目。"""
        key = (name, fingerprint)
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    def clear(self) -> None:
        """清空当前对象维护的缓存或会话状态。"""
        self._cache.clear()


SECTION_CACHE = PromptSectionCache()


def fingerprint_text(value: str) -> str:
    """计算文本内容指纹用于缓存失效。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def render_sections(sections: list[PromptSection]) -> str:
    """按顺序渲染多个 prompt 区块。"""
    return "\n\n".join(section.content.strip() for section in sections if section.content.strip())
