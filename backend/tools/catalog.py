"""Request-scoped, read-only snapshots of model-visible tools and skills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from backend.tools.contracts import freeze


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """The selected skills that the request-scoped ``Skill`` tool may execute."""

    items: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_skills(cls, skills: Iterable[dict[str, Any]]) -> "SkillCatalog":
        # Copy dictionaries so later mutation of a decoded HTTP payload cannot
        # change the discovery reminder or execution allowlist mid-run.
        return cls(tuple(freeze(item) for item in skills if _skill_enabled(item)))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(
            str(item.get("name") or item.get("id"))
            for item in self.items
            if item.get("name") or item.get("id")
        )


@dataclass(frozen=True, slots=True)
class ToolCapability:
    """Provider-visible capability metadata without retaining executors."""

    name: str
    source: str
    server_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCapabilityView:
    """The final local + MCP capability set used to compile prompt guidance."""

    capabilities: tuple[ToolCapability, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.capabilities)

    def has(self, name: str) -> bool:
        return name in self.names


def _skill_enabled(skill: dict[str, Any]) -> bool:
    return bool(skill.get("enabled", True)) and not bool(
        skill.get("disableModelInvocation") or skill.get("disable_model_invocation")
    )
