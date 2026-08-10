"""加载并按序评估 Agent 工具执行的 allow/deny/ask 规则。

pipeline：`OpenAIAgent` 在真正执行工具前调用 `check_permission(s)`；
`ask` 经 ApprovalBroker 挂起。规则文件变更靠 mtime 签名立即失效缓存。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


PermissionBehavior = Literal["allow", "deny", "ask"]

logger = logging.getLogger("k_agent.permissions")

# 源文件变更时重读规则以便立即生效；未变文件不在每次工具调用时重新解析。
_RULE_CACHE: tuple[tuple[tuple[str, int, int], ...], list["PermissionRule"]] | None = None


@dataclass(frozen=True)
class PermissionDecision:
    """解析后的行为，以及可选的人类可读命中原因。"""

    behavior: PermissionBehavior
    reason: str | None = None


@dataclass(frozen=True)
class PermissionRule:
    """一条有序通配规则：工具名 + 调用对象 pattern + 行为。"""

    tool: str
    pattern: str
    behavior: PermissionBehavior


def load_permission_rules() -> list[PermissionRule]:
    """加载用户/项目权限规则。

    文件格式刻意贴近 Claude Code：每条含 tool、pattern、behavior。
    数据驱动便于日后加 UI 编辑而不改 Agent 循环。
    """
    global _RULE_CACHE
    signature = _rule_file_signature()
    if _RULE_CACHE is not None and _RULE_CACHE[0] == signature:
        return _RULE_CACHE[1]

    rules: list[PermissionRule] = []
    for path in _rule_paths():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A malformed rule file silently disabling every deny rule in it is
            # the worst possible failure mode for a security control.
            logger.error("Ignoring unreadable permission rules at %s: %s", path, exc)
            continue
        for item in payload.get("rules", []):
            behavior = item.get("behavior")
            # 未知 behavior 直接跳过该条，不整体拒绝文件：
            # 一处笔误不应让其余规则（尤其是 deny）全部失效。
            if behavior not in {"allow", "deny", "ask"}:
                logger.info(
                    "Ignoring permission rule with unknown behavior %r in %s",
                    behavior,
                    path,
                )
                continue
            rules.append(PermissionRule(str(item.get("tool", "*")), str(item.get("pattern", "*")), behavior))
    _RULE_CACHE = (signature, rules)
    return rules


def default_behavior() -> PermissionBehavior:
    """无规则命中时的默认行为。

    默认 allow：Backend 本机 loopback、单用户场景。对外暴露部署必须设
    `K_AGENT_PERMISSION_DEFAULT=deny` 并显式写 allowlist。
    """

    configured = (os.getenv("K_AGENT_PERMISSION_DEFAULT") or "allow").strip().lower()
    if configured in {"allow", "deny", "ask"}:
        return configured  # type: ignore[return-value]
    logger.info(
        "Unknown K_AGENT_PERMISSION_DEFAULT=%r; falling back to allow", configured
    )
    return "allow"


def check_permission(tool_name: str, subject: str | None = None) -> PermissionDecision:
    """返回工具调用命中的首条 deny/allow/ask 规则（文件书写顺序即优先级）。"""
    # subject 是工具的具体调用对象（Bash 的命令、Read 的路径等），
    # 缺省时退回工具名，让只按工具粒度写的规则依然可用。
    value = subject or tool_name
    matched_ask: PermissionDecision | None = None
    for rule in load_permission_rules():
        if not _matches(rule.tool, tool_name):
            continue
        if not _matches(rule.pattern, value):
            continue
        # deny 与 allow 都立即返回，先命中者胜，所以规则文件的书写顺序即优先级。
        if rule.behavior == "deny":
            return PermissionDecision("deny", f"blocked by permission rule {rule.pattern}")
        # ask 先记下但继续扫描：后面若出现 deny，必须由更严格的它拿走决定权。
        if rule.behavior == "ask":
            matched_ask = PermissionDecision("ask", f"requires approval by permission rule {rule.pattern}")
        if rule.behavior == "allow":
            return PermissionDecision("allow", f"allowed by permission rule {rule.pattern}")
    if matched_ask is not None:
        return matched_ask
    behavior = default_behavior()
    if behavior == "allow":
        return PermissionDecision("allow")
    return PermissionDecision(
        behavior,
        f"no permission rule matches {tool_name}; default policy is {behavior}",
    )


def check_permissions(tool_name: str, subjects: list[str]) -> PermissionDecision:
    """对一次调用的多个 subject 取最严结果。

    如 ``cd /tmp && rm -rf x`` 既查整句也查分段，避免用链式绕过 ``rm *`` deny。
    """

    strictest = PermissionDecision("allow")
    rank = {"allow": 0, "ask": 1, "deny": 2}
    for subject in subjects or [tool_name]:
        decision = check_permission(tool_name, subject)
        if rank[decision.behavior] > rank[strictest.behavior]:
            strictest = decision
    return strictest


def _rule_paths() -> list[Path]:
    """返回权限规则文件的候选路径。"""
    from backend.home import permissions_path

    configured = os.getenv("K_AGENT_PERMISSION_RULES")
    paths = [Path(configured).expanduser()] if configured else []
    paths.extend([
        permissions_path(),
        # Legacy locations kept so older installs keep working until migrated.
        Path.home() / ".k_agent" / "permissions.json",
        Path.cwd() / ".k_agent" / "permissions.json",
    ])
    return paths


def _rule_file_signature() -> tuple[tuple[str, int, int], ...]:
    """快照规则文件身份（路径/mtime/size），编辑后立即失效缓存。"""

    signature: list[tuple[str, int, int]] = []
    for path in _rule_paths():
        try:
            stat = path.stat()
        except OSError:
            signature.append((str(path), -1, -1))
            continue
        signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def _matches(pattern: str, value: str) -> bool:
    """判断权限模式是否匹配工具名或对象。"""
    # 只支持 `*` 通配：先整体转义，再把转义后的 `\*` 还原成 `.*`，
    # 这样规则里的正则元字符（如路径中的 `.`）被当作字面量，
    # 用户也无法用规则文件注入任意正则。首尾锚定确保是全匹配而非子串匹配。
    regex = "^" + re.escape(pattern).replace("\\*", ".*") + "$"
    return re.match(regex, value) is not None
