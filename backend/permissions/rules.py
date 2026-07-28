from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


PermissionBehavior = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    reason: str | None = None


@dataclass(frozen=True)
class PermissionRule:
    tool: str
    pattern: str
    behavior: PermissionBehavior


def load_permission_rules() -> list[PermissionRule]:
    """Load user/project permission rules.

    The file format is intentionally close to Claude Code's rule model: each
    rule names a tool, a match pattern, and a behavior. Keeping this as data
    makes it possible to add UI editing later without changing the agent loop.
    """
    rules = []
    for path in _rule_paths():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in payload.get("rules", []):
            behavior = item.get("behavior")
            if behavior not in {"allow", "deny", "ask"}:
                continue
            rules.append(PermissionRule(str(item.get("tool", "*")), str(item.get("pattern", "*")), behavior))
    return rules


def check_permission(tool_name: str, subject: str | None = None) -> PermissionDecision:
    """Return the first matching deny/allow/ask rule for a tool invocation."""
    value = subject or tool_name
    matched_ask: PermissionDecision | None = None
    for rule in load_permission_rules():
        if not _matches(rule.tool, tool_name):
            continue
        if not _matches(rule.pattern, value):
            continue
        if rule.behavior == "deny":
            return PermissionDecision("deny", f"blocked by permission rule {rule.pattern}")
        if rule.behavior == "ask":
            matched_ask = PermissionDecision("ask", f"requires approval by permission rule {rule.pattern}")
        if rule.behavior == "allow":
            return PermissionDecision("allow", f"allowed by permission rule {rule.pattern}")
    return matched_ask or PermissionDecision("allow")


def _rule_paths() -> list[Path]:
    configured = os.getenv("K_AGENT_PERMISSION_RULES")
    paths = [Path(configured).expanduser()] if configured else []
    paths.extend([
        Path.home() / ".k_agent" / "permissions.json",
        Path.cwd() / ".k_agent" / "permissions.json",
    ])
    return paths


def _matches(pattern: str, value: str) -> bool:
    regex = "^" + re.escape(pattern).replace("\\*", ".*") + "$"
    return re.match(regex, value) is not None
