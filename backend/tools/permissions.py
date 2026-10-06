"""本地工具权限策略与 HITL 授权门；不属于 ReAct 循环。"""

from __future__ import annotations
from typing import Any
from backend.config.config import Settings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.agent.hooks import AgentRunContext
from backend.permissions import PermissionDecision, check_permissions
from backend.approvals import canonical_json_sha256
from backend.sandbox import is_domain_allowed

_READ_ONLY_LOCAL_TOOLS = frozenset({"Read", "Glob", "Grep", "LS"})
_ESCALATABLE_LOCAL_TOOLS = frozenset({"Bash", "Write", "Edit", "NotebookEdit"})


def _local_permission_decision(
    config: Settings,
    context: AgentRunContext,
    tool_name: str,
    arguments: dict[str, Any],
    subjects: tuple[str, ...],
) -> PermissionDecision:
    """本地工具的 allow / deny / ask，只出结论，不挂起、不执行。

    调用点是 Dispatcher 的 preflight：middleware wrap 之后、
    ``_enforce_permission`` 之前。``ask`` 才会进 HITL；``deny`` 变成可恢复
    工具错误；Schema 已在此之前校验，``allow`` 才进入 execute。

    分层（后者可覆盖前者，但 full_access 直接短路）：
    1. 规则文件 ``check_permissions``：按工具名 + subject（命令/路径/URL）
       取最严结果。
    2. 只读本地工具（Read/Glob/Grep/LS）把 ``ask`` 降成 ``allow``，避免
       读工作区也弹审批。``deny`` 仍生效。
    3. 本 run ``permission_mode == full_access``：一律 ``allow``，包括规则
       deny。这是会话级开关，不是单次工具授权。
    4. 默认可写工具若声明 ``sandbox_permissions=require_escalated``，强制
       ``ask``。模型不能靠这个字段自批；只是申请越权。Bash 还必须带合法
       ``escalation_scope`` + 具体 ``escalation_resource``；已在沙箱网络
       白名单里的域名直接 deny，避免无意义审批。
    """

    # 1. 规则：Bash 会拿整句 + &&/||/; 分段一起匹配，防止链式绕过 deny。
    decision = check_permissions(
        tool_name,
        subjects,
    )
    # 2. 只读探查不走 HITL；规则 deny 不能在这里被抹掉。
    # 只读工具除非明确deny，ask的情况不用管，直接开放就行
    if tool_name in _READ_ONLY_LOCAL_TOOLS and decision.behavior == "ask":
        decision = PermissionDecision(
            "allow", "read-only local access does not require HITL"
        )
    # 3. 全开：跳过规则与越权申请，后面的 sandbox 实现仍可能因 ContextVar 跳过 srt。
    if context.metadata.get("permission_mode") == "full_access":
        return PermissionDecision("allow", "full access selected for this run")
    # 未申请越权，或工具根本不能越权（Skill、WebFetch 等）：采用上面的规则结果。
    if (
        tool_name not in _ESCALATABLE_LOCAL_TOOLS
        or arguments.get("sandbox_permissions") != "require_escalated"
    ):
        return decision
    # 4. 正向越权：Write/Edit/NotebookEdit 只问一次「出沙箱」；细节在卡片参数里。
    if tool_name != "Bash":
        return PermissionDecision("ask", "该工具请求访问默认沙箱范围之外的本机资源。")

    scope = arguments.get("escalation_scope")
    resource = str(arguments.get("escalation_resource") or "").strip()
    if (
        scope
        not in {
            "outside_workspace_write",
            "host_resource",
            "network_destination",
        }
        or not resource
    ):
        return PermissionDecision(
            "deny",
            "Bash escalation requires escalation_scope and a concrete "
            "escalation_resource.",
        )
    # 白名单内域名本就可以在沙箱里访问，再申请越权视为模型误用，直接拒绝。
    if scope == "network_destination" and is_domain_allowed(
        resource, config.bash_sandbox_allowed_domains
    ):
        return PermissionDecision(
            "deny",
            f"Network destination {resource!r} is already allowed by the "
            "Bash sandbox; retry normally or adjust timeout_seconds instead "
            "of requesting escalation.",
        )
    return PermissionDecision(
        "ask",
        (
            f"该命令请求访问网络白名单外的目标：{resource}"
            if scope == "network_destination"
            else f"该命令请求访问默认沙箱外的本机资源：{resource}"
        ),
    )


async def _enforce_permission(
    runtime: dict[str, Any],
    target: str,
    decision: PermissionDecision,
    detail: dict[str, Any],
) -> None:
    """执行权限：deny 立即失败；ask 经 approval_handler 挂起等人决策。

    Resume 批准写在 runtime["_resume_authorization"]，不另开函数参数，
    避免 wrap/preflight 伪造「用户已批」。匹配 callId + 现算 hash 才放行。
    """

    approved_targets: set[str] = runtime["approved_targets"]
    approval_handler = runtime["approval_handler"]
    resume_authorization = runtime.get("_resume_authorization")
    if isinstance(resume_authorization, dict):
        detail_hash = canonical_json_sha256(
            {
                "target": detail.get("toolName") or target,
                "source": detail.get("source"),
                "serverId": detail.get("serverId"),
                "arguments": detail.get("arguments", detail.get("input", {})),
            }
        )
        if detail.get("callId") == resume_authorization.get(
            "callId"
        ) and detail_hash == resume_authorization.get("requestHash"):
            # 一次性授权在进入 execute 前消费；同 run 后续相同工具仍需重新判定。
            runtime.pop("_resume_authorization", None)
            return
    if decision.behavior == "allow" or target in approved_targets:
        return
    if decision.behavior == "ask":
        if approval_handler is None:
            raise RuntimeError(f"{target} requires manual approval")
        response = await approval_handler(target, decision, detail)
        if response.get("action") == "approve":
            if response.get("scope") == "run":
                approved_targets.add(target)
            return
        raise PermissionError(f"User denied approval for {target}")
    raise PermissionError(decision.reason or f"Permission denied for {target}")
