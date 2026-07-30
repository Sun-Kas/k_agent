"""Read, append, search, and compact the project-local automatic memory file."""

from __future__ import annotations

from pathlib import Path

from backend.memory.paths import auto_memory_dir


def get_auto_memory_entrypoint(cwd: Path | None = None) -> Path:
    """定位当前工作区的个人 memory 文件。"""
    return auto_memory_dir((cwd or Path.cwd()).resolve()) / "MEMORY.md"


def read_auto_memory(cwd: Path | None = None) -> tuple[Path, str]:
    """读取个人 memory 文件内容。"""
    path = get_auto_memory_entrypoint(cwd)
    if not path.exists():
        return path, ""
    return path, path.read_text(encoding="utf-8", errors="replace")


def append_auto_memory(text: str, cwd: Path | None = None) -> Path:
    """向个人 memory 追加一条记录。"""
    path = get_auto_memory_entrypoint(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else "# Personal Memory\n"
    separator = "" if existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}- {text.strip()}\n", encoding="utf-8")
    return path


def search_auto_memory(query: str, cwd: Path | None = None, *, limit: int = 20) -> tuple[Path, list[dict[str, str | int]]]:
    """在个人 memory 中搜索匹配条目。"""
    path, content = read_auto_memory(cwd)
    needle = query.strip().lower()
    if not needle or not content:
        return path, []
    matches = [
        {"line": index, "text": line}
        for index, line in enumerate(content.splitlines(), start=1)
        if needle in line.lower()
    ]
    return path, matches[:limit]


def compact_auto_memory(cwd: Path | None = None, *, max_items: int = 200) -> tuple[Path, int, int]:
    """压缩个人 memory 文件并保留最近条目。"""
    path, content = read_auto_memory(cwd)
    if not content:
        return path, 0, 0
    header: list[str] = []
    items: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not items:
            header.append(stripped)
            continue
        item = stripped[2:].strip() if stripped.startswith("- ") else stripped
        key = " ".join(item.lower().split())
        if key and key not in seen:
            seen.add(key)
            items.append(item)
    compacted_items = items[-max_items:]
    new_content = "\n".join(header or ["# Personal Memory"])
    new_content += "\n" + "\n".join(f"- {item}" for item in compacted_items) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")
    return path, len(items), len(compacted_items)
