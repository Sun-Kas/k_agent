"""唯一工具执行入口：Middleware 的每次重写都重新解析、校验并鉴权。"""

from __future__ import annotations
import asyncio
import copy
import uuid
from backend.permissions import PermissionDecision, check_permission
from backend.tools.contracts import (
    ToolOutcome,
    ToolProtocolError,
    ToolOutcomeStatus,
    ToolCallRequest,
    ToolExecutionContext,
    ProviderToolCall,
    ToolKey,
    ActivateToolAllowlist,
    RecordInvokedSkill,
    UpdatePlan,
    UpdateWorkingSet,
    RecordLoadedMemoryPaths,
    ToolBindingDependencies,
    thaw,
)
from backend.tools.normalization import (
    _decode_tool_arguments,
    _recoverable_tool_error,
)
from backend.tools.permissions import _local_permission_decision, _enforce_permission
from backend.tools.validation import validate_tool_arguments
from backend.tools.streaming import emit_tool_output


class ToolDispatcher:
    def __init__(
        self,
        tool_set,
        *,
        normalizer=None,
        workspace=None,
        network_access=None,
        dependencies=None,
    ):
        self.tool_set = tool_set
        self.normalizer = normalizer
        self.dependencies = dependencies or ToolBindingDependencies(
            workspace, network_access
        )

    def checkpoint_state(self, runtime):
        state = runtime.get("tool_effect_state", {})
        allowed = state.get("allowlist")
        return {
            "owner": state.get("owner"),
            "allowed": None
            if allowed is None
            else [
                {"source": k.source, "name": k.name, "namespace": k.namespace}
                for k in sorted(
                    allowed, key=lambda k: (k.source, k.namespace or "", k.name)
                )
            ],
        }

    def restore_state(self, runtime, checkpoint):
        scope = checkpoint.get("toolScope")
        if not isinstance(scope, dict):
            return
        allowed = scope.get("allowed")
        keys = (
            None if allowed is None else frozenset(ToolKey(**item) for item in allowed)
        )
        available = {b.spec.key for b in self.tool_set.bindings}
        if keys is not None and not keys <= available:
            raise ValueError("Checkpoint tool scope is no longer available")
        runtime["tool_effect_state"] = {"allowlist": keys, "owner": scope.get("owner")}

    def resume_outcome(self, runtime, name, arguments, decision, payload):
        # 与首次调用使用同一个 normalizer，别名恢复也不能绕开目录。
        request = self._request(ProviderToolCall("", 0, name, arguments), _decode_tool_arguments(arguments))
        binding = self._resolve(request)
        if binding.resume is not None:
            return binding.resume(runtime, name, thaw(request.arguments), decision, payload)
        approved = decision.get("status") == "resolved" and isinstance(payload, dict) and payload.get("approved") is True
        if approved:
            return None
        return ToolOutcome.failed(
            code="ApprovalCancelled" if decision.get("status") == "cancelled" else "ApprovalDenied",
            message="User denied or cancelled the approval request", kind="permission",
            status=ToolOutcomeStatus.CANCELLED if decision.get("status") == "cancelled" else ToolOutcomeStatus.DENIED,
            content=f"Tool {name} was not executed because the user denied or cancelled the approval request.",
        )

    def _resolve(self, request):
        # source/server/canonical 全部是不可信 middleware 输入，必须命中同一快照。
        return self.tool_set.resolve_key(ToolKey(request.source, request.canonical_name, request.server_id))

    def _request(self, call, arguments):
        name = call.requested_name
        if self.normalizer is not None:
            name, arguments = self.normalizer(name, arguments, self.tool_set)
        binding = self.tool_set.resolve(name)
        key = binding.spec.key
        return ToolCallRequest(
            call.call_id or str(uuid.uuid4()),
            call.iteration,
            call.requested_name,
            key.name,
            arguments,
            key.source,
            key.namespace,
        )

    def _apply_effects(self, runtime, outcome):
        if outcome.status != ToolOutcomeStatus.SUCCEEDED:
            return
        # 全部 effect 先在副本校验，提交前不改 allowlist 或 working set。
        state = copy.deepcopy(runtime.get("tool_effect_state", {}))
        working = copy.deepcopy(
            runtime.get(
                "working_set", {"recentFiles": [], "invokedSkillIds": [], "plan": None}
            )
        )
        loaded = set(runtime.get("loaded_memory_paths", ()))
        available = {b.spec.key for b in self.tool_set.bindings}
        for effect in outcome.effects:
            if isinstance(effect, ActivateToolAllowlist):
                if not effect.allowed <= available:
                    raise ToolProtocolError(
                        "Tool allowlist contains unavailable tool keys"
                    )
                state.update(allowlist=effect.allowed, owner=effect.owner)
            elif isinstance(effect, RecordInvokedSkill):
                working["invokedSkillIds"] = list(
                    dict.fromkeys(
                        [*working.get("invokedSkillIds", []), effect.skill_id]
                    )
                )
            elif isinstance(effect, UpdatePlan):
                working["plan"] = thaw(effect.todos)
            elif isinstance(effect, UpdateWorkingSet):
                working["recentFiles"] = thaw(effect.files)
            elif isinstance(effect, RecordLoadedMemoryPaths):
                loaded.update(effect.paths)
            else:
                raise ToolProtocolError(f"Unknown tool effect: {type(effect).__name__}")
        runtime.update(tool_effect_state=state, working_set=working)
        # Agent 已持有该集合引用；无 await 的提交段保持引用稳定。
        runtime.setdefault("loaded_memory_paths", set()).update(loaded)

    async def dispatch(self, runtime, call: ProviderToolCall) -> ToolOutcome:
        pipeline = runtime["pipeline"]
        try:
            arguments = _decode_tool_arguments(call.arguments_json)
            request = self._request(call, arguments)
            if request.source == "local" and request.canonical_name != call.requested_name:
                metadata = pipeline.context.metadata
                metadata["toolAliasNormalizationCount"] = metadata.get("toolAliasNormalizationCount", 0) + 1
        except Exception as exc:
            await pipeline.emit_failure(
                exc,
                stage="tool_resolve",
                detail={"toolName": call.requested_name, "callId": call.call_id},
            )
            return self._failure(call.requested_name, exc)

        async def preflight(current):
            binding = self._resolve(current)
            arguments = thaw(current.arguments)
            validate_tool_arguments(binding.spec.input_schema, arguments)
            context = pipeline.context
            state = runtime.get("tool_effect_state", {})
            allowed = state.get("allowlist")
            if (
                context.metadata.get("permission_mode") != "full_access"
                and allowed is not None
                and binding.spec.key not in allowed
            ):
                raise PermissionError(
                    f"Skill {state.get('owner')} restricts tool use to the active allowlist; {binding.spec.provider_name} is not permitted while this skill is active."
                )
            if binding.prepare is not None:
                await binding.prepare(runtime, current)
            subjects = binding.permission_subjects(arguments)
            if binding.spec.key.source == "local":
                decision = _local_permission_decision(
                    runtime["config"],
                    context,
                    binding.spec.provider_name,
                    arguments,
                    subjects,
                )
                target = binding.spec.provider_name
                detail = {
                    "toolName": current.requested_name,
                    "callId": current.call_id,
                    "iteration": current.iteration,
                    "arguments": arguments,
                    "source": "local",
                }
            else:
                decision = (
                    PermissionDecision("allow", "full access selected for this run")
                    if context.metadata.get("permission_mode") == "full_access"
                    else check_permission("mcp", subjects[0])
                )
                target = (
                    f"MCP tool {binding.spec.key.namespace}:{binding.spec.key.name}"
                )
                detail = {
                    "toolName": binding.spec.key.name,
                    "callId": current.call_id,
                    "iteration": current.iteration,
                    "serverId": binding.spec.key.namespace,
                    "arguments": arguments,
                    "source": "mcp",
                }
            await _enforce_permission(runtime, target, decision, detail)

        async def execute(current):
            binding = self._resolve(current)
            context = ToolExecutionContext(
                request_id=pipeline.context.request_id or "",
                run_id=pipeline.context.run_id or "",
                workspace=self.dependencies.workspace,
                network_access=self.dependencies.network_access,
                permission_mode=pipeline.context.metadata.get(
                    "permission_mode", "default"
                ),
                output_limits=self.dependencies.output_limits,
                emit_output=emit_tool_output,
            )
            try:
                timeout = binding.spec.execution_policy.timeout_seconds
                if timeout is None:
                    outcome = await binding.execute(context, current.arguments)
                else:
                    async with asyncio.timeout(timeout):
                        outcome = await binding.execute(context, current.arguments)
                if not isinstance(outcome, ToolOutcome):
                    raise ToolProtocolError("Tool executor must return ToolOutcome")
                if outcome.status == ToolOutcomeStatus.SUCCEEDED:
                    enricher = runtime.get("observation_enricher")
                    if enricher is not None and binding.spec.key.source == "local":
                        outcome = await enricher(
                            binding.spec.provider_name, thaw(current.arguments), outcome
                        )
                self._apply_effects(runtime, outcome)
                return outcome
            except Exception as exc:
                return self._failure(binding.spec.provider_name, exc)

        try:
            result = await pipeline.run_tool(
                request, preflight=preflight, execute=execute
            )
            outcome = result.outcome
        except Exception as exc:
            outcome = self._failure(call.requested_name, exc)
        runtime.setdefault("_tool_outcomes", {})[call.call_id] = outcome
        return outcome

    @staticmethod
    def _failure(name, exc):
        return ToolOutcome.failed(
            code=type(exc).__name__,
            message=str(exc),
            kind="protocol" if isinstance(exc, ToolProtocolError) else "permission"
            if isinstance(exc, PermissionError)
            else "input"
            if isinstance(exc, ValueError)
            else "execution",
            retryable=isinstance(exc, TimeoutError),
            status=ToolOutcomeStatus.DENIED
            if isinstance(exc, PermissionError)
            else ToolOutcomeStatus.FAILED,
            content=_recoverable_tool_error(tool_name=name, error=exc),
        )
