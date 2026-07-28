from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    cacheable: bool = True


class PromptSectionCache:
    """Small in-process cache for stable prompt sections.

    Dynamic sections such as connected MCP tools should be rebuilt every request;
    static sections can be reused until settings or memory state changes.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], str] = {}

    def get(self, name: str, fingerprint: str, compute: Callable[[], str]) -> str:
        key = (name, fingerprint)
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    def clear(self) -> None:
        self._cache.clear()


SECTION_CACHE = PromptSectionCache()


def fingerprint_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def render_sections(sections: list[PromptSection]) -> str:
    return "\n\n".join(section.content.strip() for section in sections if section.content.strip())
