"""Parse memory frontmatter, includes, rules, and bounded automatic-memory content."""

from __future__ import annotations

import re
from pathlib import Path

from backend.memory.constants import MAX_AUTOMEM_ENTRYPOINT_CHARS, MAX_AUTOMEM_ENTRYPOINT_LINES
from backend.memory.models import MemoryType, ParsedMemory


def parse_memory(raw: str, memory_type: MemoryType) -> ParsedMemory:
    """解析 memory 文件的 frontmatter、include 和正文。"""
    content = _strip_block_html_comments(raw)
    frontmatter, content = _split_frontmatter(content)
    globs = tuple(_frontmatter_globs(frontmatter))
    content_without_frontmatter = content
    if memory_type == MemoryType.AUTOMATED:
        content = _truncate_auto_memory(content)
    includes = tuple(_extract_includes(content))
    return ParsedMemory(
        content=content.strip(),
        globs=globs,
        includes=includes,
        content_differs_from_disk=content != content_without_frontmatter or bool(frontmatter),
    )


def resolve_include(include_path: str, base_dir: Path) -> Path:
    """根据当前文件目录解析 include 路径。"""
    if include_path.startswith("~/"):
        return Path.home() / include_path[2:]
    if include_path.startswith("/"):
        return Path(include_path)
    return base_dir / include_path


def _split_frontmatter(content: str) -> tuple[str, str]:
    """拆分 markdown frontmatter 和正文。"""
    if not content.startswith("---\n"):
        return "", content
    end = content.find("\n---", 4)
    if end == -1:
        return "", content
    after = end + len("\n---")
    if after < len(content) and content[after] == "\n":
        after += 1
    return content[4:end], content[after:]


def _frontmatter_globs(frontmatter: str) -> list[str]:
    """解析 frontmatter 中的路径 glob。"""
    globs: list[str] = []
    capture = False
    for raw_line in frontmatter.splitlines():
        line = raw_line.strip()
        if re.match(r"^(paths|globs)\s*:", line):
            capture = True
            inline = line.split(":", 1)[1].strip()
            if inline.startswith("[") and inline.endswith("]"):
                globs.extend(item.strip().strip("\"'") for item in inline[1:-1].split(",") if item.strip())
            elif inline:
                globs.append(inline.strip("\"'"))
            continue
        if capture and line.startswith("-"):
            globs.append(line[1:].strip().strip("\"'"))
        elif line and not line.startswith("#"):
            capture = False
    return [glob for glob in globs if glob]


def _extract_includes(content: str) -> list[str]:
    """提取 memory 文件中的 include 指令。"""
    includes: list[str] = []
    in_fenced_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fenced_block = not in_fenced_block
            continue
        if in_fenced_block:
            continue
        match = re.match(r"^@([^\s`]+)\s*$", stripped)
        if match:
            includes.append(match.group(1))
    return includes


def _strip_block_html_comments(content: str) -> str:
    """移除 memory 正文中的 HTML 注释块。"""
    return re.sub(r"(?ms)^<!--.*?-->\s*", "", content)


def _truncate_auto_memory(content: str) -> str:
    """限制自动 memory 注入到 prompt 的长度。"""
    lines = content.splitlines()
    truncated = False
    if len(lines) > MAX_AUTOMEM_ENTRYPOINT_LINES:
        lines = lines[:MAX_AUTOMEM_ENTRYPOINT_LINES]
        truncated = True
    content = "\n".join(lines)
    if len(content) > MAX_AUTOMEM_ENTRYPOINT_CHARS:
        content = content[:MAX_AUTOMEM_ENTRYPOINT_CHARS].rstrip()
        truncated = True
    if truncated:
        content += "\n\n[Auto memory truncated. Use memory tools or files to inspect the full memory.]"
    return content
