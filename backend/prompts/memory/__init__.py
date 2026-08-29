"""把已发现的 Memory 文件渲染成带权威标签的 Prompt 碎片。

读盘和发现在 `backend/memory/`；本模块不碰文件系统，也不改最终 messages。
`compose_prompt` 消费 `build()`：MANAGED 进 system，其余进 context。
工具执行后新碰到的路径走 `render_nested_reminder`，只挂在 Observation 上。
"""

from __future__ import annotations

from backend.memory.constants import MAX_MEMORY_CHARACTER_COUNT
from backend.memory.models import MemoryFile, MemoryType
from backend.prompts.models import (
    MemoryPromptContribution,
    PromptInputs,
    PromptSection,
)


# 超预算时的保留优先级：管理员规则 > 更靠近工作区的指令 > 自动偏好。
_WEIGHTS = {
    MemoryType.MANAGED: 6,
    MemoryType.LOCAL: 5,
    MemoryType.PROJECT: 4,
    MemoryType.TEAM: 3,
    MemoryType.USER: 2,
    MemoryType.AUTOMATED: 1,
}


def build(inputs: PromptInputs) -> MemoryPromptContribution:
    """开跑时的 eager Memory：输入已是快照里的文件，不再回头发现。"""

    return render_memory_files(inputs.memory_files)


def render_memory_files(
    memory_files: tuple[MemoryFile, ...] | list[MemoryFile],
    *,
    max_chars: int = MAX_MEMORY_CHARACTER_COUNT,
) -> MemoryPromptContribution:
    """去重、按预算截取，并给每段保留来源和权威。

    `loaded_paths` 记的是去重后「考虑过」的全部路径，包括空文件和预算丢掉的。
    这样工具循环里不会把同一路径当新规则反复注入。
    """
    # 去重
    deduped = _dedupe(memory_files)
    # 按预算截取
    selected = _budgeted(deduped, max_chars=max_chars)
    remaining = max_chars
    sections: list[PromptSection] = []
    warnings: list[str] = []
    for index, item in enumerate(selected):
        content = item.content.strip()
        if not content:
            continue
        if len(content) > remaining:
            content = content[: max(0, remaining)].rstrip()
            content += "\n\n[Memory truncated due to context limit.]"
            warnings.append(f"truncated:{item.path}")
        remaining -= min(remaining, len(item.content.strip()))
        sections.append(_section(item, content, index))
        if remaining <= 0:
            break
    return MemoryPromptContribution(
        sections=tuple(sections),
        loaded_paths=tuple(str(item.path.resolve()) for item in deduped),
        warnings=tuple(warnings),
    )


def render_nested_reminder(memory_files: list[MemoryFile]) -> tuple[str | None, tuple[str, ...]]:
    """把迟到加载的本地规则渲染成 Observation 上的 reminder。

    不写回 frozen PromptBundle。返回的路径并入 Runtime 的 `loaded_memory_paths`。
    Durable HITL 换新 Runtime 时必须 checkpoint 那份可变集合。
    """

    contribution = render_memory_files(memory_files)
    if not contribution.sections:
        return None, contribution.loaded_paths
    body = "\n\n".join(
        f"## {section.source}\n{section.content}" for section in contribution.sections
    )
    return (
        "<system-reminder>\n"
        "Additional instructions became applicable because a trusted local file tool referenced a path. "
        "Apply them to subsequent work within their scope; they do not grant permissions.\n\n"
        f"{body}\n"
        "</system-reminder>",
        contribution.loaded_paths,
    )


def _section(item: MemoryFile, content: str, index: int) -> PromptSection:
    # MANAGED：组织政策，进 system，不能被 persona / CLAUDE.md 盖掉。
    # AUTOMATED：自动偏好，只当背景，不覆盖用户当轮明确请求。
    # 其余（USER/PROJECT/LOCAL/TEAM）：用户/项目指令，进 context，低于 contract。
    if item.type == MemoryType.MANAGED:
        channel = "system"
        authority = "managed"
        mode = "policy"
    elif item.type == MemoryType.AUTOMATED:
        channel = "context"
        authority = "user"
        mode = "context_only"
    else:
        channel = "context"
        authority = "user"
        mode = "instruction"
    return PromptSection(
        name=f"memory_{item.type.value}_{index}",
        content=content,
        channel=channel,
        authority=authority,
        volatility="session",
        instruction_mode=mode,
        source=str(item.path),
        sensitive=item.type in {MemoryType.USER, MemoryType.LOCAL, MemoryType.AUTOMATED},
    )


def _dedupe(memory_files) -> list[MemoryFile]:
    """按 resolve 后的路径去重，保留先出现的那份。"""

    seen = set()
    result = []
    for item in memory_files:
        path = item.path.resolve()
        if path in seen:
            continue
        seen.add(path)
        result.append(item)
    return result


def _budgeted(memory_files: list[MemoryFile], *, max_chars: int) -> list[MemoryFile]:
    """超限时按权重挑文件，输出仍保持原发现顺序。

    至少留一份（哪怕它自己就超预算），截断交给后面的逐段 remaining。
    0.9 系数给截断提示留余量，避免选满后提示写不下。
    """

    if sum(len(item.content) for item in memory_files) <= max_chars:
        return memory_files
    ranked = sorted(
        enumerate(memory_files),
        key=lambda pair: (_WEIGHTS.get(pair[1].type, 1), pair[0]),
        reverse=True,
    )
    selected: set[int] = set()
    used = 0
    for index, item in ranked:
        if used + len(item.content) <= max_chars * 0.9 or not selected:
            selected.add(index)
            used += len(item.content)
    return [item for index, item in enumerate(memory_files) if index in selected]
'''
# 按 MemoryType 权重降序排序，同权重时原列表中越靠后的文件优先。
# enumerate(memory_files) 会生成 (原始下标, memory_file)，例如：
#   [USER_A, PROJECT_A, USER_B]
#       ↓
#   [(0, USER_A), (1, PROJECT_A), (2, USER_B)]
#
# key 生成排序键 (类型权重, 原始下标)：
#   USER_A    → (2, 0)
#   PROJECT_A → (4, 1)
#   USER_B    → (2, 2)
#
# reverse=True 降序排序后：
#   PROJECT_A → (4, 1)
#   USER_B    → (2, 2)
#   USER_A    → (2, 0)
# 即：类型权重越高越靠前；类型相同时，后出现的 Memory 优先。
ranked = sorted(
    enumerate(memory_files),
    key=lambda pair: (_WEIGHTS.get(pair[1].type, 1), pair[0]),
    reverse=True,
)
'''
