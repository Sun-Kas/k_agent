"""Request-scoped, read-only snapshots of model-visible tools and skills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from backend.mcp_tool import McpToolDescriptor
from backend.tools.local import ToolDefinition


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """The selected skills that the request-scoped ``Skill`` tool may execute."""

    items: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_skills(cls, skills: Iterable[dict[str, Any]]) -> "SkillCatalog":
        # Copy dictionaries so later mutation of a decoded HTTP payload cannot
        # change the discovery reminder or execution allowlist mid-run.
        return cls(tuple(dict(item) for item in skills if _skill_enabled(item)))

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
class ToolCatalog:
    """The final local + MCP capability set used to compile prompt guidance."""

    capabilities: tuple[ToolCapability, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.capabilities)

    def has(self, name: str) -> bool:
        return name in self.names


def build_tool_catalog(
    *,
    local_tools: Iterable[ToolDefinition],
    mcp_tools: Iterable[McpToolDescriptor],
) -> ToolCatalog:
    """Build the capability snapshot after all request filtering and binding."""

    capabilities = [
        ToolCapability(name=tool.name, source="local") for tool in local_tools
    ]
    capabilities.extend(
        ToolCapability(
            name=f"mcp__{tool.server_id}__{tool.name}",
            source="mcp",
            server_id=tool.server_id,
        )
        for tool in mcp_tools
    )
    return ToolCatalog(tuple(capabilities))


def _skill_enabled(skill: dict[str, Any]) -> bool:
    return bool(skill.get("enabled", True)) and not bool(
        skill.get("disableModelInvocation") or skill.get("disable_model_invocation")
    )
