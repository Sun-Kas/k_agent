"""Render request-scoped Skill discovery metadata as context, not tool schema."""

from __future__ import annotations

from dataclasses import dataclass

from backend.prompts.models import (
    PromptInputs,
    PromptSection,
    SkillPromptContribution,
)


# 只约束发现列表（name + description）；SKILL.md 正文仍走 Skill 工具 observation。
# 总预算 = context_window_tokens × CHARS_PER_TOKEN × 本比例（当前 2%）。
SKILL_BUDGET_CONTEXT_PERCENT = 0.02
# 把 token 窗口换成字符的粗估（英文约 4 字/token；中文更密，同样字符更吃窗口）。
CHARS_PER_TOKEN = 4
# 模型配置没有 context_window 时的硬顶，避免按未知窗口无限灌列表。
DEFAULT_CHAR_BUDGET = 15_000
# 单条 listing 的 description 上限（含拼上的 whenToUse），不是正文。
MAX_LISTING_DESC_CHARS = 400
# 超总预算后均分到每条的额度低于此值：丢掉简介，只留名称（Skill 工具 id）。
MIN_DESC_LENGTH = 20


@dataclass(frozen=True, slots=True)
class _ListingEntry:
    name: str
    description: str
    truncated: bool = False


def build(inputs: PromptInputs) -> SkillPromptContribution:
    """Build one discovery Section from the exact Catalog bound to execution."""

    # Catalog 非空但 Skill 工具未暴露时不能提示模型调用；Provider tools 才是能力真相。
    if not inputs.tool_catalog.has("Skill"):
        return SkillPromptContribution(())

    # 获取到skill_catalog的items
    entries = tuple(
        entry
        for item in inputs.skill_catalog.items
        if (entry := _listing_entry(item)) is not None
    )
    if not entries:
        return SkillPromptContribution(())

    lines, truncated_count = _format_within_budget(
        entries,
        _char_budget(inputs.context_window_tokens),
    )
    content = (
        "The following skills are available for use with the `Skill` tool. "
        "This list is capability discovery, not authorization. Invoke a matching "
        "skill by its exact name before continuing with the task; do not guess names:\n\n"
        + "\n".join(lines)
    )
    section = PromptSection(
        name="available_skills",
        content=content,
        channel="context",
        authority="user",
        volatility="request",
        instruction_mode="context_only",
        source="runner_context.skills",
    )
    return SkillPromptContribution(
        sections=(section,),
        listing_chars=len(content),
        included_count=len(entries),
        truncated_count=truncated_count,
    )


def _char_budget(context_window_tokens: int | None) -> int:
    if isinstance(context_window_tokens, int) and context_window_tokens > 0:
        return int(
            context_window_tokens
            * CHARS_PER_TOKEN
            * SKILL_BUDGET_CONTEXT_PERCENT
        )
    return DEFAULT_CHAR_BUDGET


def _listing_entry(item: dict[str, object]) -> _ListingEntry | None:
    """Discovery line from catalog metadata only; never SKILL.md `instructions`."""
    name = str(item.get("name") or item.get("id") or "").strip()
    if not name:
        return None

    description = str(item.get("description") or "").strip()
    when_to_use = str(
        item.get("whenToUse") or item.get("when_to_use") or ""
    ).strip()
    if when_to_use and when_to_use not in description:
        description = (
            f"{description} - {when_to_use}" if description else when_to_use
        )

    truncated = len(description) > MAX_LISTING_DESC_CHARS
    if truncated:
        description = description[: MAX_LISTING_DESC_CHARS - 1] + "…"
    return _ListingEntry(name=name, description=description, truncated=truncated)


def _format_within_budget(
    entries: tuple[_ListingEntry, ...], # 每个skill item的 metadata
    budget: int,                        # 预算，即context_window_tokens * CHARS_PER_TOKEN * SKILL_BUDGET_CONTEXT_PERCENT
) -> tuple[tuple[str, ...], int]:
    full_lines = tuple(_format_entry(entry) for entry in entries)
    full_chars = sum(len(line) for line in full_lines) + max(0, len(entries) - 1)
    if full_chars <= budget:
        return full_lines, sum(entry.truncated for entry in entries)

    # 超预算时不丢 skill：名称是 Skill 工具的精确 id，只能砍每条 description。
    # 一行完整形态是 `- {name}: {description}`。固定开销 = 名称 + "- " + ": "（4 字符）。
    # 行与行之间还有 n-1 个换行，也要从总预算里扣掉，剩下的才均分给简介。
    name_overhead = (
        sum(len(entry.name) + 4 for entry in entries) + max(0, len(entries) - 1)
    )
    # 整数除法：每条简介同一上限。有的条本身更短，实际总长会略小于预算。
    max_description_length = (budget - name_overhead) // len(entries)
    # 均分后连一句有用的简介都写不下：退化为只列名称，避免半截字和猜名。
    if max_description_length < MIN_DESC_LENGTH:
        names_only_lines = tuple(f"- {entry.name}" for entry in entries)
        truncated_count = sum(bool(entry.description) or entry.truncated for entry in entries)
        return names_only_lines, truncated_count

    formatted_lines: list[str] = []
    truncated_count = 0
    for entry in entries:
        description = entry.description
        truncated = entry.truncated
        if len(description) > max_description_length:
            # 预留 1 字符给省略号，使该行长度不超过均分上限。
            description = description[: max_description_length - 1] + "…"
            truncated = True
        if truncated:
            truncated_count += 1
        formatted_lines.append(_format_entry(_ListingEntry(entry.name, description)))
    return tuple(formatted_lines), truncated_count


def _format_entry(entry: _ListingEntry) -> str:
    if not entry.description:
        return f"- {entry.name}"
    return f"- {entry.name}: {entry.description}"
