"""在 Skill 工具确认调用后，按请求中的安全 ID 懒加载 SKILL.md 正文。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.home import skills_dir


# 与 HTTP catalog ID 契约一致；拒绝路径分隔符和上级目录片段。
_SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")

# 防止手工放入异常大的 SKILL.md 后，一次工具调用挤爆模型上下文。
MAX_SKILL_MARKDOWN_BYTES = 512 * 1024

# 测试可以注入临时目录；生产始终使用 Backend 自己解析的 K_AGENT_HOME。
DATA_SKILLS_DIR: Path | None = None


class SkillBodyError(ValueError):
    """Skill ID、包路径或正文不符合懒加载契约。"""


@dataclass(frozen=True, slots=True)
class SkillBody:
    """已加载正文及其可信包路径；不包含任何 frontmatter 元数据。"""

    instructions: str
    file_path: Path
    base_dir: Path


def load_skill_body(skill_id: str) -> SkillBody:
    """从 Backend Skill 根目录读取正文，但绝不解析或返回 YAML 字段。"""

    normalized = str(skill_id or "").strip()
    if not _SKILL_ID.fullmatch(normalized):
        raise SkillBodyError(f"Invalid Skill id: {normalized!r}")

    root = (DATA_SKILLS_DIR or skills_dir()).resolve()
    candidate = (root / normalized / "SKILL.md").resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SkillBodyError(f'Skill path escapes the managed root: "{normalized}"') from exc
    if not candidate.is_file():
        raise SkillBodyError(f'Skill instructions not found: "{normalized}"')
    try:
        size = candidate.stat().st_size
        if size > MAX_SKILL_MARKDOWN_BYTES:
            raise SkillBodyError(
                f'Skill instructions exceed {MAX_SKILL_MARKDOWN_BYTES} bytes: "{normalized}"'
            )
        raw = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SkillBodyError(f'Cannot read Skill instructions: "{normalized}"') from exc

    instructions = _body_after_frontmatter(raw).strip()
    if not instructions:
        raise SkillBodyError(f'Skill instructions are empty: "{normalized}"')
    return SkillBody(
        instructions=instructions,
        file_path=candidate,
        base_dir=candidate.parent,
    )


def _body_after_frontmatter(content: str) -> str:
    """只定位 YAML 围栏并返回正文；运行时禁止解释其中任何 metadata。"""

    if not content.startswith("---\n"):
        return content
    end = content.find("\n---", 4)
    if end == -1:
        return content
    body = content[end + len("\n---") :]
    return body[1:] if body.startswith("\n") else body
