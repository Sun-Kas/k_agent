"""内置 OpenAI 兼容 React Agent Runner（默认 agentKind=`k_agent`）。

pipeline：按请求连接 MCP → 拼 prompt → 绑定本轮工具 → 创建 Agent Runtime
→ 内部事件。会话状态仍在 Access Layer；Agent 单例不保存请求状态。
"""

from __future__ import annotations

import copy
import asyncio
import hashlib
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from backend.agent import AgentRunRequest, OpenAIAgent
from backend.agent.hooks import AgentRunContext
from backend.agent.hooks.builtins import build_k_agent_pipeline_definition
from backend.mcp_tool import mcp_manager_from_runtime
from backend.memory import (
    load_eager_memory,
    load_fresh_nested_memory,
    resolve_instruction_root,
    trusted_tool_paths,
)
from backend.prompts import (
    McpInstruction,
    PersonaInputs,
    PromptInputs,
    compose_prompt,
)
from backend.prompts.memory import render_nested_reminder
from backend.runners.base import RunnerContext
from backend.runtime_config import normalize_reasoning_effort, select_compact_model, select_model
from backend.context.projection import render_working_set
from backend.tools import (
    SkillCatalog,
    bind_request_scoped_tools,
    build_tool_catalog,
    load_local_tools,
)
from backend.permissions import PermissionDecision


logger = logging.getLogger("k_agent.runners.k_agent")


def _observed_file_digest(path: Path) -> str | None:
    """只记录可信本地路径的内容摘要；失败不把文件正文塞进 state。"""

    try:
        if not path.is_file():
            return None
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class KAgentRunner:
    """默认 Agent 单例；请求状态只存在于 create_runtime 的函数作用域。"""

    __slots__ = ("_agent", "_pipeline_definition")
    kind = "k_agent"

    def __init__(self) -> None:
        # OpenAIAgent 本身不保存请求数据，可安全地随 Runner 一起进程内复用。
        self._agent = OpenAIAgent()
        self._pipeline_definition = build_k_agent_pipeline_definition()

    async def create_runtime(
        self, ctx: RunnerContext
    ) -> dict[str, Any]:
        """组装请求级 Runtime 信息；不执行模型循环。"""
        # 内置 Runner 依赖进程级配置、MCP 连接池和观测运行时；缺任一就不能安全开跑。
        if ctx.settings is None or ctx.mcp_pool is None or ctx.langfuse is None:
            raise RuntimeError("KAgentRunner requires settings, mcp_pool, and langfuse")

        settings = ctx.settings
        # MCP 日志与本轮请求对齐，便于按 request/thread/run 追查连接失败。
        log_context = {
            "requestId": ctx.request_id or "-",
            "threadId": ctx.thread_id,
            "runId": ctx.run_id,
        }
        # 按本轮勾选的 server 建 manager，会话租约仍复用 ctx.mcp_pool，避免每请求新建进程连接。
        mcp_manager = mcp_manager_from_runtime(
            ctx.mcp_servers,
            log_context=log_context,
            connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
            call_timeout_seconds=settings.mcp_call_timeout_seconds,
            max_call_retries=settings.mcp_call_max_retries,
            retry_base_delay_seconds=settings.mcp_call_retry_base_delay_seconds,
            session_pool=ctx.mcp_pool,
        )
        try:
            # 先连上再 list_tools / instructions；失败走 except，归还租约。
            await mcp_manager.connect_all()
            # 请求未指定模型时回落到 settings 默认；返回的是本轮要用的模型目录项。
            model = select_model(ctx.model_id, settings)
            compact_model = select_compact_model(model, settings)
            # 把对话里所有附件摊平成一份清单，用来校验模型是否吃得下这些模态。
            media = [
                attachment
                for message in ctx.messages
                for attachment in message.attachments
            ]
            # 优先读 inputModalities；旧配置只有 multimodal 布尔时，按图文或纯文本兜底。
            modalities = set(
                model.get("inputModalities")
                or (
                    ["text", "image"]
                    if model.get("multimodal", False)
                    else ["text"]
                )
            )
            # item.type 是 IANA MIME（浏览器 File.type / Data URL），如 video/mp4、image/png；
            # 模型目录用粗粒度 inputModalities: "image" | "video"。video/ 前缀来自 MIME 顶级类型，
            # 不是本仓库自创。Access Layer 只放行 image/ 与 video/，所以 else 一律当 image；
            # 纯文本在 messages.content，不会出现在 attachments 里。
            required = {
                "video" if item.type.startswith("video/") else "image"
                for item in media
            }
            # 集合差集：附件需要的模态 − 模型声明的 inputModalities。
            # 空集表示都能吃；非空（如 {"video"}）说明当前模型缺这类输入，立刻失败。
            unsupported = required - modalities
            if unsupported:
                raise ValueError(
                    "Selected model does not support "
                    f"{', '.join(sorted(unsupported))} input"
                )
            skills = ctx.skills
            # Access Layer 下发的已选 MCP id；后面过滤 instructions，并写入 run_request。
            selected_mcp_ids = {
                str(server.get("id"))
                for server in ctx.mcp_servers
                if server.get("id")
            }
            # 已连接 server 的工具快照。能力本身只通过 Provider tools 暴露；
            # Prompt 只读取最终 Catalog 生成少量条件指导。
            mcp_tools = await mcp_manager.list_tools()
            instruction_root = resolve_instruction_root(
                settings.local_tool_workspace_root
            )
            # Memory discovery is blocking filesystem work and must stay off the
            # streaming event loop. It is rooted at the project, never at the
            # session/Team artifact workspace.
            memory_files = await asyncio.to_thread(
                load_eager_memory,
                instruction_root,
                ctx.messages,
            )
            skill_catalog = SkillCatalog.from_skills(skills)
            tools = bind_request_scoped_tools(
                await load_local_tools(),
                mcp_manager,
                skill_catalog=skill_catalog,
            )
            tool_catalog = build_tool_catalog(
                local_tools=tools,
                mcp_tools=mcp_tools,
            )
            # InitializeResult.instructions for selected servers — not tool descriptions.
            instructions = tuple(
                McpInstruction(server_id=server_id, content=value)
                for server_id, value in mcp_manager.connected_instructions().items()
                if (not selected_mcp_ids or server_id in selected_mcp_ids) and value
            )
            prompt_started_at = time.perf_counter()
            # All K Agent injected text is compiled here. The Runner supplies a
            # frozen snapshot and never edits the resulting bundle.
            prompt_bundle = compose_prompt(
                PromptInputs(
                    instruction_root=instruction_root,
                    output_workspace=ctx.workspace_dir,
                    memory_files=tuple(memory_files),
                    tool_catalog=tool_catalog,
                    skill_catalog=skill_catalog,
                    context_window_tokens=(
                        model.get("contextWindow")
                        if isinstance(model.get("contextWindow"), int)
                        else None
                    ),
                    mcp_instructions=instructions,
                    mcp_servers=tuple(dict(server) for server in ctx.mcp_servers),
                    persona=PersonaInputs(custom=settings.persona_override),
                    permission_mode=(
                        "full_access"
                        if ctx.options.get("permissionMode") == "full_access"
                        else "default"
                    ),
                    options=dict(ctx.options),
                    team_id=ctx.team_id,
                    cache_breaker=(
                        os.getenv("K_AGENT_SYSTEM_PROMPT_INJECTION")
                        or os.getenv("CLAUDE_CODE_SYSTEM_PROMPT_INJECTION")
                    ),
                )
            )
            # 模型循环的输入快照：消息/prompt/模型/权限都在这里，create_runtime 本身不跑 Agent。
            run_request = AgentRunRequest(
                # 会话历史（含用户输入）；不是 user_context。Access Layer 已按 carries_context 过滤。
                messages=ctx.messages,
                model_config=model,
                context_summary=ctx.context_summary,
                context_state=copy.deepcopy(ctx.context_state),
                continuation_checkpoint=copy.deepcopy(ctx.continuation_checkpoint),
                working_set_context=render_working_set(
                    ctx.context_state,
                    allowed_roots=[instruction_root, ctx.workspace_dir],
                    authorized_skill_ids={
                        str(skill.get("id") or "") for skill in skills if skill.get("id")
                    },
                    context_window=int(model.get("contextWindow") or 128_000),
                ),
                prompt=prompt_bundle,
                attachments=ctx.attachments,
                mcp_server_ids=selected_mcp_ids,
                reasoning_effort=normalize_reasoning_effort(
                    model, ctx.reasoning_effort
                ),
                permission_mode=(
                    "full_access"
                    if ctx.options.get("permissionMode") == "full_access"
                    else "default"
                ),
            )
            # 请求级 Observer 在真正打开 Langfuse trace 后再统一绑定。
            observers = [item for item in (ctx.logging_observer,) if item is not None]
            # 把后续 run_stream 需要的句柄一次性交出去；MCP 租约所有权随之转移。
            return {
                "settings": settings,
                "mcp_manager": mcp_manager,
                "model": model,
                "compact_model": compact_model,
                "skills": skills,
                "tools": tools,
                "run_request": run_request,
                "instruction_root": instruction_root,
                "output_workspace": ctx.workspace_dir,
                "observers": observers,
                "observability_metadata": {
                    "mcpServerIds": sorted(selected_mcp_ids),
                    "skillIds": [
                        str(skill.get("id") or skill.get("name"))
                        for skill in skills
                        if skill.get("id") or skill.get("name")
                    ],
                    "reasoningEffort": run_request.reasoning_effort,
                    "loadedMemoryPathCount": len(prompt_bundle.initial_memory_paths),
                    "stablePromptFingerprint": prompt_bundle.stable_fingerprint,
                    "dynamicPromptFingerprint": prompt_bundle.dynamic_fingerprint,
                    "skillListingChars": prompt_bundle.skill_listing_chars,
                    "skillListingCount": prompt_bundle.skill_listing_count,
                    "skillListingTruncatedCount": (
                        prompt_bundle.skill_listing_truncated_count
                    ),
                    "requestId": ctx.request_id,
                    "promptComposeMs": round(
                        (time.perf_counter() - prompt_started_at) * 1000, 3
                    ),
                    "agentKind": self.kind,
                },
            }
        except BaseException:
            # 还没把 Runtime 交给 run_stream：这里必须还租约。成功返回后改由
            # run_stream 的 finally 关闭，流被取消时也不会泄漏 MCP session。
            await mcp_manager.close_all()
            raise

    async def run_stream(
        self, ctx: RunnerContext
    ) -> AsyncIterator[dict[str, Any]]:
        """加载 Runtime 信息，在这里执行观察与模型工具循环。"""
        runtime = await self.create_runtime(ctx)

        mcp_manager = runtime["mcp_manager"]
        agent_runtime_ref: dict[str, Any] = {}

        async def enrich_observation(
            tool_name: str,
            arguments: dict[str, Any],
            tool_result: str,
        ) -> str:
            """Load scoped rules from trusted local tool arguments only."""

            agent_runtime = agent_runtime_ref.get("value")
            if not isinstance(agent_runtime, dict):
                return tool_result
            working_set = agent_runtime.setdefault(
                "working_set",
                copy.deepcopy(ctx.context_state.get("workingSet") or {
                    "recentFiles": [], "invokedSkillIds": [], "plan": None
                }),
            )
            if tool_name == "Skill":
                skill_id = str(arguments.get("skill") or "").strip().lstrip("/")
                invoked = list(working_set.get("invokedSkillIds") or [])
                if skill_id and skill_id not in invoked:
                    working_set["invokedSkillIds"] = [*invoked, skill_id]
            if tool_name == "TodoWrite" and isinstance(arguments.get("todos"), list):
                working_set["plan"] = copy.deepcopy(arguments["todos"])
            paths = trusted_tool_paths(
                tool_name,
                arguments,
                instruction_root=runtime["instruction_root"],
                tool_workspace=runtime["output_workspace"],
            )
            if not paths:
                return tool_result
            recent_files = list(working_set.get("recentFiles") or [])
            by_path = {
                str(item.get("path")): item
                for item in recent_files
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
            for raw_path in paths:
                path = Path(raw_path)
                digest = await asyncio.to_thread(_observed_file_digest, path)
                by_path[str(path)] = {
                    "path": str(path),
                    "observedDigest": digest,
                }
            working_set["recentFiles"] = list(by_path.values())[-5:]
            loaded: set[str] = agent_runtime["loaded_memory_paths"]
            fresh = await asyncio.to_thread(
                load_fresh_nested_memory,
                paths,
                instruction_root=runtime["instruction_root"],
                loaded_paths=set(loaded),
            )
            reminder, loaded_paths = render_nested_reminder(fresh)
            loaded.update(loaded_paths)
            if reminder is None:
                return tool_result
            agent_runtime["trace"].append(
                f"memory:lazy_loaded:{len(loaded_paths)} files"
            )
            return f"{tool_result}\n\n{reminder}"

        async def request_approval(
            target: str,
            decision: PermissionDecision,
            detail: dict[str, Any],
        ) -> dict[str, Any]:
            """把 ReAct preflight 的 ask 接到本轮 HTTP 的 ApprovalBroker。

            OpenAIAgent 只认识这个回调，不认识 thread/run/Broker。这里组好
            checkpoint 后调用 ``broker.request()``：往当前流塞 Interrupt，然后
            ``await run_closed``。返回值不是用户决定（那是另一次 Resume Run）；
            原 Run 随后被取消，K Agent 不会拿这里的返回值去执行工具。
            """
            if ctx.approval_broker is None:
                raise RuntimeError("Approval broker is unavailable")
            # create_runtime 时尚无检查点；Act 写入 _react_tool_boundary 之后
            # 才会第一次 ask。用 ref 是因为闭包必须在 create_runtime 之前定义。
            agent_runtime = agent_runtime_ref.get("value")
            boundary = (
                copy.deepcopy(agent_runtime.get("_react_tool_boundary"))
                if isinstance(agent_runtime, dict)
                and isinstance(agent_runtime.get("_react_tool_boundary"), dict)
                else {"version": 1, "kind": "restart_from_context"}
            )
            # AG-UI MESSAGES_SNAPSHOT 用请求级消息；Resume Act 用 boundary 里的
            # modelMessages。两者不能混成一份列表。
            boundary["messages"] = [
                message.model_dump(by_alias=True, mode="json")
                for message in ctx.messages
            ]
            return await ctx.approval_broker.request(
                thread_id=ctx.thread_id,
                run_id=ctx.run_id,
                agent_kind=self.kind,
                category=(
                    "user_input"
                    if detail.get("source") == "user_input"
                    else "mcp_tool" if detail.get("source") == "mcp" else "local_tool"
                ),
                title=(
                    "Agent 需要你的回答"
                    if detail.get("source") == "user_input"
                    else f"允许调用 {target}？"
                ),
                message=(
                    str((detail.get("questions") or [{}])[0].get("question") or decision.reason)
                    if detail.get("source") == "user_input"
                    else decision.reason or "该工具调用需要你的确认。"
                ),
                detail=detail,
                checkpoint=boundary,
            )

        try:
            with ctx.langfuse.observe_agent_run(
                session_id=ctx.thread_id,
                run_id=ctx.run_id,
                model=str(
                    runtime["model"].get("model") or ctx.model_id or "unknown"
                ),
                messages=ctx.messages,
                metadata=runtime["observability_metadata"],
            ) as observability_observers:
                run_context = AgentRunContext(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    run_id=ctx.run_id,
                    metadata={"permission_mode": runtime["run_request"].permission_mode},
                )
                agent_runtime = await self._agent.create_runtime(
                    runtime["run_request"],
                    runtime["tools"],
                    mcp_manager,
                    observers=[
                        *runtime["observers"],
                        *observability_observers,
                    ],
                    pipeline_definition=self._pipeline_definition,
                    run_context=run_context,
                    config=runtime["settings"],
                    skills=runtime["skills"],
                    approval_handler=request_approval,
                )
                # The generic Agent core does not import Prompt/Memory modules;
                # this request callback owns K Agent's lazy-rule semantics.
                agent_runtime["observation_enricher"] = enrich_observation
                agent_runtime["working_set"] = copy.deepcopy(
                    ctx.context_state.get("workingSet") or {
                        "recentFiles": [], "invokedSkillIds": [], "plan": None
                    }
                )
                agent_runtime_ref["value"] = agent_runtime
                if ctx.resume_checkpoints:
                    # Access Layer 已验证 resume 完整覆盖开放 Interrupt；K Agent
                    # 只接受一个 ReAct 工具边界，避免把多个决议错误合并成一次执行。
                    if len(ctx.resume_checkpoints) != 1:
                        raise RuntimeError("K Agent resume requires exactly one checkpoint")
                    resume_record = ctx.resume_checkpoints[0]
                    checkpoint = resume_record.get("checkpoint")
                    decision = resume_record.get("decision")
                    if not isinstance(checkpoint, dict) or not isinstance(decision, dict):
                        raise RuntimeError("Invalid K Agent resume checkpoint")
                    agent_runtime["resume_checkpoint"] = copy.deepcopy(checkpoint)
                    agent_runtime["resume_decision"] = copy.deepcopy(decision)
                    agent_runtime["resume_request_hash"] = resume_record.get("requestHash")
                if isinstance(ctx.continuation_checkpoint, dict):
                    agent_runtime["continuation_checkpoint"] = copy.deepcopy(
                        ctx.continuation_checkpoint
                    )
                agent_runtime["compact_model"] = runtime["compact_model"]
                async for event in self._agent.run_stream_react(agent_runtime):
                    yield event
        finally:
            await mcp_manager.close_all()
