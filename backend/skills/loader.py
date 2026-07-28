from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from backend.memory.rules import matches_rule
from backend.prompts.lifecycle import reset_prompt_caches
from backend.skills.frontmatter import parse_bool, parse_markdown_frontmatter, split_frontmatter_list


@dataclass(frozen=True)
class SkillDefinition:
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
        return bool(self.paths)


class SkillRegistry:
    """Caches always-visible skills and stores conditional skills separately.

    This mirrors Claude Code's behavior: path-filtered skills do not pollute the
    initial tool prompt. They become available only after the model/user touches
    a matching file path, at which point prompt caches are cleared.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._cache: dict[str, list[SkillDefinition]] = {}
        self._conditional: dict[str, SkillDefinition] = {}
        self._dynamic: dict[str, SkillDefinition] = {}
        self._checked_dynamic_dirs: set[str] = set()

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._conditional.clear()
            self._dynamic.clear()
            self._checked_dynamic_dirs.clear()

    def get(self, cwd: Path) -> list[SkillDefinition]:
        key = str(cwd.resolve())
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return [*cached, *self._dynamic.values()]
        loaded = _load_all_skills(cwd)
        unconditional = []
        conditional = {}
        for skill in _dedupe_skills(loaded):
            if skill.is_conditional:
                conditional[skill.name] = skill
            else:
                unconditional.append(skill)
        with self._lock:
            self._cache[key] = unconditional
            self._conditional.update(conditional)
            return [*unconditional, *self._dynamic.values()]

    def activate_for_paths(self, paths: list[Path], cwd: Path) -> list[SkillDefinition]:
        activated: list[SkillDefinition] = []
        with self._lock:
            for skill in list(self._conditional.values()):
                if any(matches_rule(path, skill.paths, Path(skill.base_dir or cwd)) for path in paths):
                    self._dynamic[skill.name] = skill
                    activated.append(skill)
            for skill in activated:
                self._conditional.pop(skill.name, None)
        # Nested skill discovery lets a subdirectory carry its own skills. We
        # remember both hits and misses so repeated file reads do not restat the
        # same directories on every tool loop.
        dynamic_dirs = _discover_nested_skill_dirs(paths, cwd, self._checked_dynamic_dirs)
        for directory in dynamic_dirs:
            for skill in _load_skills_dir(directory, "project", "skills"):
                with self._lock:
                    self._dynamic[skill.name] = skill
                    activated.append(skill)
        if activated:
            reset_prompt_caches("skills_activated")
        return activated


SKILL_REGISTRY = SkillRegistry()


def load_skill_registry(cwd: Path | None = None) -> list[SkillDefinition]:
    return SKILL_REGISTRY.get((cwd or Path.cwd()).resolve())


def get_available_skills(cwd: Path | None = None) -> list[SkillDefinition]:
    return load_skill_registry(cwd)


def activate_skills_for_paths(paths: list[Path], cwd: Path | None = None) -> list[SkillDefinition]:
    return SKILL_REGISTRY.activate_for_paths(paths, (cwd or Path.cwd()).resolve())


def clear_skill_caches() -> None:
    SKILL_REGISTRY.clear()
    reset_prompt_caches("skills_cache_clear")


def mcp_prompt_to_skill(server_id: str, prompt: dict[str, Any]) -> SkillDefinition:
    """Represent an MCP prompt as a model-visible skill summary."""
    name = _normalize_skill_name(f"mcp__{server_id}__{prompt.get('name', 'prompt')}")
    description = str(prompt.get("description") or f"MCP prompt from {server_id}")
    arguments = [
        item.get("name")
        for item in prompt.get("arguments", [])
        if isinstance(item, dict) and item.get("name")
    ]
    return SkillDefinition(
        id=name,
        name=name,
        description=description,
        content=f"Invoke MCP prompt {prompt.get('name')} from server {server_id}.",
        source="mcp",
        loaded_from="mcp",
        argument_names=tuple(arguments),
        user_invocable=False,
    )


def _load_all_skills(cwd: Path) -> list[SkillDefinition]:
    skills: list[SkillDefinition] = []
    if not _truthy(os.getenv("K_AGENT_DISABLE_MANAGED_SKILLS")):
        skills.extend(_load_skills_dir(_managed_skills_dir(), "managed", "skills"))
    if not _truthy(os.getenv("K_AGENT_DISABLE_USER_SKILLS")):
        skills.extend(_load_skills_dir(_user_skills_dir(), "user", "skills"))
    if not _truthy(os.getenv("K_AGENT_DISABLE_PROJECT_SKILLS")):
        for directory in reversed([cwd, *cwd.parents]):
            skills.extend(_load_skills_dir(directory / ".k_agent" / "skills", "project", "skills"))
            skills.extend(_load_skills_dir(directory / ".claude" / "skills", "project", "skills"))
            skills.extend(_load_legacy_commands_dir(directory / ".k_agent" / "commands", "project"))
            skills.extend(_load_legacy_commands_dir(directory / ".claude" / "commands", "project"))
    for directory in _additional_skill_dirs():
        skills.extend(_load_skills_dir(directory, "project", "skills"))
    return skills


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


def _load_legacy_commands_dir(base_path: Path, source: str) -> list[SkillDefinition]:
    """Load older markdown command files as skills for compatibility."""
    if not base_path.exists() or not base_path.is_dir():
        return []
    skills = []
    for path in sorted(base_path.rglob("*.md")):
        name = path.parent.name if path.name.lower() == "skill.md" else path.stem
        skill = _parse_skill_file(path, name, source, "commands_DEPRECATED", path.parent)
        if skill:
            skills.append(skill)
    return skills


def _parse_skill_file(path: Path, default_name: str, source: str, loaded_from: str, base_dir: Path) -> SkillDefinition | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    frontmatter, content = parse_markdown_frontmatter(raw)
    name = _normalize_skill_name(str(frontmatter.get("name") or default_name))
    description = str(frontmatter.get("description") or _extract_description(content, name))
    return SkillDefinition(
        id=name,
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
    seen_paths: set[str] = set()
    by_name: dict[str, SkillDefinition] = {}
    for skill in skills:
        identity = str(Path(skill.file_path).resolve()) if skill.file_path else skill.name
        if identity in seen_paths:
            continue
        seen_paths.add(identity)
        by_name[skill.name] = skill
    return list(by_name.values())


def _discover_nested_skill_dirs(paths: list[Path], cwd: Path, checked: set[str]) -> list[Path]:
    dirs: list[Path] = []
    cwd = cwd.resolve()
    for path in paths:
        current = path.expanduser().resolve().parent
        while str(current).startswith(str(cwd) + os.sep):
            for candidate in (current / ".k_agent" / "skills", current / ".claude" / "skills"):
                key = str(candidate)
                if key in checked:
                    continue
                checked.add(key)
                if candidate.is_dir():
                    dirs.append(candidate)
            parent = current.parent
            if parent == current:
                break
            current = parent
    return sorted(dirs, key=lambda item: len(item.parts), reverse=True)


def _managed_skills_dir() -> Path:
    return Path(os.getenv("K_AGENT_MANAGED_SKILLS_DIR", "/etc/k-agent/.k_agent/skills")).expanduser()


def _user_skills_dir() -> Path:
    return Path(os.getenv("K_AGENT_SKILLS_DIR", Path.home() / ".k_agent" / "skills")).expanduser()


def _additional_skill_dirs() -> list[Path]:
    raw = os.getenv("K_AGENT_ADDITIONAL_SKILL_DIRECTORIES", "")
    return [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]


def _extract_description(content: str, fallback: str) -> str:
    for line in content.splitlines():
        stripped = line.strip("# ").strip()
        if stripped:
            return stripped[:240]
    return fallback


def _normalize_skill_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_:-]+", "-", name.strip())
    return normalized.strip("-") or "skill"


def _truthy(value: str | None) -> bool:
    return value is not None and value.lower() not in {"", "0", "false", "no", "off"}
