"""Discover, parse, deduplicate, cache, and activate project and user Skills."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from backend.home import skills_dir
from backend.memory.rules import matches_rule
from backend.prompts.lifecycle import reset_prompt_caches
from backend.skills.frontmatter import parse_bool, parse_markdown_frontmatter, split_frontmatter_list

# Tests may patch this to a temporary directory; production uses $K_AGENT_HOME.
DATA_SKILLS_DIR: Path | None = None


def skills_storage_dir() -> Path:
    """Resolved Skill package root (`$K_AGENT_HOME/content/skills`)."""

    return DATA_SKILLS_DIR if DATA_SKILLS_DIR is not None else skills_dir()


@dataclass(frozen=True)
class SkillDefinition:
    """Parsed Skill metadata, instructions, and optional activation conditions."""

    id: str
    name: str
    description: str
    content: str
    source: str
    loaded_from: str
    file_path: str | None = None
    base_dir: str | None = None
    allowed_tools: tuple[str, ...] = ()
    argument_hint: str | None = None
    argument_names: tuple[str, ...] = ()
    when_to_use: str | None = None
    version: str | None = None
    model: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    execution_context: str = "inline"
    agent: str | None = None
    paths: tuple[str, ...] = ()
    hooks: dict[str, Any] = field(default_factory=dict)

    @property
    def is_conditional(self) -> bool:
        """判断 Skill 是否需要按路径条件触发。"""
        return bool(self.paths)


class SkillRegistry:
    """Caches always-visible skills and stores conditional skills separately.

    This mirrors Claude Code's behavior: path-filtered skills do not pollute the
    initial tool prompt. They become available only after the model/user touches
    a matching file path, at which point prompt caches are cleared.
    """

    def __init__(self) -> None:
        """初始化对象依赖和内部状态。"""
        # 这是进程级共享状态，可能被多个并发请求同时访问，用 RLock 而非
        # asyncio.Lock：加载过程是同步文件 IO，且 activate 内部会重入。
        self._lock = RLock()
        # _cache 无条件 Skill；_conditional 等待路径命中；
        # _dynamic 已被路径激活、后续一直可见。
        self._cache: dict[str, list[SkillDefinition]] = {}
        self._conditional: dict[str, SkillDefinition] = {}
        self._dynamic: dict[str, SkillDefinition] = {}

    def clear(self) -> None:
        """清空当前对象维护的缓存或会话状态。"""
        with self._lock:
            self._cache.clear()
            self._conditional.clear()
            self._dynamic.clear()

    def get(self, cwd: Path) -> list[SkillDefinition]:
        """读取或创建当前对象管理的条目。"""
        key = str(skills_storage_dir().resolve())
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return [*cached, *self._dynamic.values()]
        # 刻意在锁外做文件加载：这是一段较慢的同步 IO，持锁会阻塞其他请求。
        # 代价是并发首次访问可能重复加载一次，结果相同，可以接受。
        loaded = _load_all_skills(cwd)
        unconditional = []
        conditional = {}
        for skill in _dedupe_skills(loaded):
            if skill.is_conditional:
                conditional[skill.id] = skill
            else:
                unconditional.append(skill)
        with self._lock:
            self._cache[key] = unconditional
            self._conditional.update(conditional)
            return [*unconditional, *self._dynamic.values()]

    def activate_for_paths(self, paths: list[Path], cwd: Path) -> list[SkillDefinition]:
        """把路径条件命中的 Skill 从待激活集合移入常驻可见集合。"""
        activated: list[SkillDefinition] = []
        with self._lock:
            # 迭代 list() 快照而非字典本身：循环内会修改 _conditional。
            for skill in list(self._conditional.values()):
                if any(matches_rule(path, skill.paths, Path(skill.base_dir or cwd)) for path in paths):
                    self._dynamic[skill.id] = skill
                    activated.append(skill)
            for skill in activated:
                self._conditional.pop(skill.id, None)
        # 可用 Skill 集合变了，缓存的系统提示词已经过时，必须清掉重建。
        if activated:
            reset_prompt_caches("skills_activated")
        return activated


SKILL_REGISTRY = SkillRegistry()


def load_skill_registry(cwd: Path | None = None) -> list[SkillDefinition]:
    """Return all unconditional and previously activated Skills."""

    return SKILL_REGISTRY.get((cwd or Path.cwd()).resolve())


def get_available_skills(cwd: Path | None = None) -> list[SkillDefinition]:
    """返回当前工作区可被模型使用的 Skill 列表。"""
    return load_skill_registry(cwd)


def activate_skills_for_paths(paths: list[Path], cwd: Path | None = None) -> list[SkillDefinition]:
    """Activate conditional Skills whose path rules match referenced files."""

    return SKILL_REGISTRY.activate_for_paths(paths, (cwd or Path.cwd()).resolve())


def clear_skill_caches(reason: str = "skills_cache_clear") -> None:
    """清空全局 Skill 加载缓存。"""
    SKILL_REGISTRY.clear()
    reset_prompt_caches(reason)


def _load_all_skills(cwd: Path) -> list[SkillDefinition]:
    """从 `$K_AGENT_HOME/content/skills` 加载全部 Skill。"""
    del cwd
    return _load_skills_dir(skills_storage_dir(), "home", "skills")


def _load_skills_dir(base_path: Path, source: str, loaded_from: str) -> list[SkillDefinition]:
    """Load directory-format skills: skills/name/SKILL.md only."""
    if not base_path.exists() or not base_path.is_dir():
        return []
    skills = []
    for entry in sorted(base_path.iterdir(), key=lambda item: item.name):
        skill_file = entry / "SKILL.md"
        if not entry.is_dir() or not skill_file.is_file():
            continue
        skill = _parse_skill_file(skill_file, entry.name, source, loaded_from, entry)
        if skill:
            skills.append(skill)
    return skills


def _parse_skill_file(path: Path, default_name: str, source: str, loaded_from: str, base_dir: Path) -> SkillDefinition | None:
    """读取并解析单个 SKILL.md 文件。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    frontmatter, content = parse_markdown_frontmatter(raw)
    skill_id = _normalize_skill_name(default_name)
    name = str(frontmatter.get("name") or default_name).strip() or skill_id
    description = str(frontmatter.get("description") or _extract_description(content, name))
    return SkillDefinition(
        id=skill_id,
        name=name,
        description=description,
        content=content.strip(),
        source=source,
        loaded_from=loaded_from,
        file_path=str(path.resolve()),
        base_dir=str(base_dir.resolve()),
        allowed_tools=tuple(split_frontmatter_list(frontmatter.get("allowed-tools"))),
        argument_hint=str(frontmatter.get("argument-hint")) if frontmatter.get("argument-hint") else None,
        argument_names=tuple(split_frontmatter_list(frontmatter.get("arguments"))),
        when_to_use=str(frontmatter.get("when_to_use")) if frontmatter.get("when_to_use") else None,
        version=str(frontmatter.get("version")) if frontmatter.get("version") else None,
        model=str(frontmatter.get("model")) if frontmatter.get("model") and frontmatter.get("model") != "inherit" else None,
        disable_model_invocation=parse_bool(frontmatter.get("disable-model-invocation"), False),
        user_invocable=parse_bool(frontmatter.get("user-invocable"), True),
        execution_context="fork" if frontmatter.get("context") == "fork" else "inline",
        agent=str(frontmatter.get("agent")) if frontmatter.get("agent") else None,
        paths=tuple(split_frontmatter_list(frontmatter.get("paths"))),
        hooks=frontmatter.get("hooks") if isinstance(frontmatter.get("hooks"), dict) else {},
    )


def _dedupe_skills(skills: list[SkillDefinition]) -> list[SkillDefinition]:
    """按 Skill ID 去重并保留优先项。"""
    # 两层去重：先按真实文件路径跳过同一文件的重复来源，
    # 再按 id 收敛，使同名 Skill 中后加载的（优先级更高的来源）覆盖先加载的。
    seen_paths: set[str] = set()
    by_name: dict[str, SkillDefinition] = {}
    for skill in skills:
        identity = str(Path(skill.file_path).resolve()) if skill.file_path else skill.name
        if identity in seen_paths:
            continue
        seen_paths.add(identity)
        by_name[skill.id] = skill
    return list(by_name.values())


def _extract_description(content: str, fallback: str) -> str:
    """从 Skill 正文中提取描述兜底值。"""
    for line in content.splitlines():
        stripped = line.strip("# ").strip()
        if stripped:
            return stripped[:240]
    return fallback


def _normalize_skill_name(name: str) -> str:
    """把 Skill 名称规整为稳定 ID。"""
    # ID 会作为函数名候选出现在模型的工具视野里，也会拼进文件路径，
    # 因此白名单只保留字母数字和少量安全符号，其余（含路径分隔符）一律替换。
    normalized = re.sub(r"[^a-zA-Z0-9_:-]+", "-", name.strip())
    return normalized.strip("-") or "skill"
