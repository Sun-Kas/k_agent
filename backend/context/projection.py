"""压缩后工作集重建：可信文件、Skill 使用记录和结构化 plan。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from backend.context.manager import estimate_text_tokens


def render_working_set(
    state: dict[str, Any],
    *,
    allowed_roots: Iterable[Path],
    authorized_skill_ids: set[str],
    context_window: int,
) -> str:
    """重新读取仍在可信根目录内的近期文件；不读取 Skill 正文。"""

    working = state.get("workingSet")
    if not isinstance(working, dict):
        return ""
    roots = [root.resolve() for root in allowed_roots]
    total_budget = min(50_000, int(context_window * 0.15))
    used = 0
    sections: list[str] = []
    for item in list(working.get("recentFiles") or [])[-5:]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = Path(item["path"]).expanduser().resolve()
        if not any(_within(path, root) for root in roots):
            sections.append(f"- File unavailable outside trusted roots: {path}")
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            sections.append(f"- File unavailable; read again if needed: {path}")
            continue
        # 单文件 5k token，且总预算受模型窗口约束。超出时明确要求模型重读。
        if estimate_text_tokens(content) > 5_000 or used + estimate_text_tokens(content) > total_budget:
            sections.append(f"- File requires a fresh Read for exact content: {path}")
            continue
        used += estimate_text_tokens(content)
        sections.append(f"## Recent file: {path}\n```\n{content}\n```")
    invoked = [
        str(item) for item in working.get("invokedSkillIds") or []
        if isinstance(item, str) and item in authorized_skill_ids
    ]
    if invoked:
        sections.append(
            "## Previously invoked Skills\n"
            + ", ".join(invoked)
            + "\nInvoke Skill again if exact instructions are required."
        )
    plan = working.get("plan")
    if plan is not None:
        sections.append("## Current structured plan\n" + json.dumps(plan, ensure_ascii=False))
    return "\n\n".join(sections)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
