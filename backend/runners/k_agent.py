"""内置 OpenAI 兼容 React Agent Runner（默认 agentKind=`k_agent`）。

pipeline：按请求连接 MCP → 拼 prompt → 绑定本轮工具 → 创建 Agent Runtime
→ 内部事件。会话状态仍在 Access Layer；Agent 单例不保存请求状态。
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, cast

from backend.agent import AgentRunRequest, OpenAIAgent
from backend.agent.hooks import AgentRunContext
from backend.agent.hooks.builtins import build_k_agent_pipeline_definition
from backend.mcp_tool import mcp_manager_from_runtime
from backend.prompts import (
    build_prompt_bundle,
    extract_referenced_paths,
    voice_conversation_prompt,
)
from backend.runners.base import RunnerContext
from backend.runtime_config import normalize_reasoning_effort, select_model
from backend.tools import bind_request_scoped_tools, load_local_tools
from backend.permissions import PermissionDecision


logger = logging.getLogger("k_agent.runners.k_agent")


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
            # 已连接 server 的工具快照，供 prompt 告诉模型「这轮能调哪些 MCP」。
            mcp_tools = await mcp_manager.list_tools()
            # 从消息里抽出被 @ 的路径，prompt 才会把对应本地文件/目录编进上下文。
            referenced_paths = extract_referenced_paths(ctx.messages)
            prompt_started_at = time.perf_counter()
            # 拼本轮 system + user_context；Skills / 语音提示都只活在这次请求里。
            prompt_bundle = build_prompt_bundle(
                settings.system_prompt,
                # Prompt helpers also accept SkillDefinition. This request carries
                # only serialized Skill dictionaries from the Access Layer.
                skills=cast(Any, skills),
                # Voice guidance is rebuilt per run and never becomes a
                # persisted chat message or process-global prompt mutation.
                append_system_prompt=voice_conversation_prompt(ctx.options),
                referenced_paths=referenced_paths,
                mcp_tools=cast(list[Any], mcp_tools),
            )
            # 拷一份再改：workspace / MCP 列表 / server instructions 都是本轮附加，不写回 bundle。
            # user_context 是给模型看的 旁路环境字典，不是用户输入
            user_context = dict(prompt_bundle.user_context)
            if ctx.workspace_dir is not None:
                # 与请求级本地工具同一边界。显式写入 prompt，避免 Skill 仍往 /tmp 写。
                workspace = str(ctx.workspace_dir)
                if ctx.team_id:
                    user_context["workingDirectory"] = (
                        f"{workspace}\n"
                        "这是当前 Team 任务工作区（不是普通对话的 session 协作区）。"
                        "正式交付物写在此目录下的 output/；"
                        "不要写到 /tmp、仓库根目录、其他 Team 任务目录或对话 session 目录。"
                        "相对路径相对此任务目录解析。"
                    )
                else:
                    user_context["workingDirectory"] = (
                        f"{workspace}\n"
                        "这是当前对话的 session 协作区（每个会话独立，不同于 Team 任务目录）。"
                        "生成的报告、脚本 --output-file、下载文件和其他交付物都必须写在这个目录内；"
                        "不要写到 /tmp、仓库根目录、其他会话目录或 Team 目录。"
                        "相对路径也相对此目录解析。"
                    )
            # 把用户勾选的 MCP 名称/说明列进上下文，模型才知道本轮启用了哪些服务。
            if ctx.mcp_servers:
                user_context["selectedMcpServers"] = "\n".join(
                    f"- {server.get('name') or server.get('id')}: "
                    f"{server.get('description') or ''}".rstrip()
                    for server in ctx.mcp_servers
                )
            # MCP initialize 带回的动态指令只属于本轮；未勾选的 server 即使连上也丢掉。
            instructions = {
                server_id: value
                for server_id, value in mcp_manager.connected_instructions().items()
                if not selected_mcp_ids or server_id in selected_mcp_ids
            }
            if instructions:
                user_context["mcpInstructions"] = "\n\n".join(
                    f"## {server_id}\n\n{value}"
                    for server_id, value in instructions.items()
                )
            # 本地工具 + 本轮 MCP + Skills 绑成请求级工具表；不要挂到 Runner 单例上。
            tools = bind_request_scoped_tools(
                await load_local_tools(), mcp_manager, skills
            )
            # 模型循环的输入快照：消息/prompt/模型/权限都在这里，create_runtime 本身不跑 Agent。
            run_request = AgentRunRequest(
                # 会话历史（含用户输入）；不是 user_context。Access Layer 已按 carries_context 过滤。
                messages=ctx.messages,
                # 本轮 system：人设、Skill 摘要、MCP 动态清单、语音附加指令等。
                system_prompt=prompt_bundle.system_prompt,
                # 旁路环境字典（日期/memory/工作区/MCP 说明），渲染成 system-reminder，不是用户原话。
                user_context=user_context,
                model_config=model,
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
                loaded_memory_paths=prompt_bundle.memory_paths,
            )
            # 请求级 Observer 在真正打开 Langfuse trace 后再统一绑定。
            observers = [item for item in (ctx.logging_observer,) if item is not None]
            # 把后续 run_stream 需要的句柄一次性交出去；MCP 租约所有权随之转移。
            return {
                "settings": settings,
                "mcp_manager": mcp_manager,
                "model": model,
                "skills": skills,
                "tools": tools,
                "run_request": run_request,
                "observers": observers,
                "observability_metadata": {
                    "mcpServerIds": sorted(selected_mcp_ids),
                    "skillIds": [
                        str(skill.get("id") or skill.get("name"))
                        for skill in skills
                        if skill.get("id") or skill.get("name")
                    ],
                    "reasoningEffort": run_request.reasoning_effort,
                    "loadedMemoryPathCount": len(prompt_bundle.memory_paths),
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

        async def request_approval(
            target: str,
            decision: PermissionDecision,
            detail: dict[str, Any],
        ) -> dict[str, Any]:
            if ctx.approval_broker is None:
                raise RuntimeError("Approval broker is unavailable")
            agent_runtime = agent_runtime_ref.get("value")
            boundary = (
                copy.deepcopy(agent_runtime.get("_react_tool_boundary"))
                if isinstance(agent_runtime, dict)
                and isinstance(agent_runtime.get("_react_tool_boundary"), dict)
                else {"version": 1, "kind": "restart_from_context"}
            )
            boundary["messages"] = [
                message.model_dump(by_alias=True, mode="json")
                for message in ctx.messages
            ]
            return await ctx.approval_broker.request(
                thread_id=ctx.thread_id,
                run_id=ctx.run_id,
                agent_kind=self.kind,
                category=(
                    "mcp_tool" if detail.get("source") == "mcp" else "local_tool"
                ),
                title=f"允许调用 {target}？",
                message=decision.reason or "该工具调用需要你的确认。",
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
                async for event in self._agent.run_stream_react(agent_runtime):
                    yield event
        finally:
            await mcp_manager.close_all()
