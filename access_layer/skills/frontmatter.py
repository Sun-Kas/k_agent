"""YAML-like Skill frontmatter. Used only when writing catalog (import / save / first bootstrap).

Run-time listing and Agent Backend requests read catalog JSON, not this parser.
"""

from __future__ import annotations

import re
from typing import Any


def markdown_body_after_frontmatter(content: str) -> str:
    """Drop the YAML fence; do not parse keys."""
    _raw, body = _split_frontmatter_block(content)
    return body


def parse_markdown_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """拆分 SKILL.md YAML 和正文。仅导入、配置保存、catalog 初次生成使用。"""
    raw, body = _split_frontmatter_block(content)
    if raw is None:
        return {}, content
    return _parse_simple_yaml(raw), body


def _split_frontmatter_block(content: str) -> tuple[str | None, str]:
    if not content.startswith("---\n"):
        return None, content
    end = content.find("\n---", 4)
    if end == -1:
        return None, content
    raw = content[4:end]
    body = content[end + len("\n---") :]
    if body.startswith("\n"):
        body = body[1:]
    return raw, body


def split_frontmatter_list(value: Any) -> list[str]:
    """把 frontmatter 字段转换为字符串列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        return [item.strip().strip("\"'") for item in text[1:-1].split(",") if item.strip()]
    return [item.strip() for item in re.split(r"[,;\n]", text) if item.strip()]


def parse_bool(value: Any, default: bool = False) -> bool:
    """按常见字符串规则解析布尔配置。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_simple_yaml(raw: str) -> dict[str, Any]:
    """解析当前支持的简单 YAML frontmatter。"""
    result: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current_key and stripped.startswith("-"):
            result.setdefault(current_key, []).append(stripped[1:].strip().strip("\"'"))
            continue
        if ":" not in stripped:
            current_key = None
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if not value:
            result[key] = []
        else:
            result[key] = value.strip("\"'")
    return result
