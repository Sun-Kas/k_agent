"""Built-in OpenAI-compatible React agent runner."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any, cast

from backend.agent import AgentRunRequest, OpenAIAgent
from backend.mcp_tool import mcp_manager_from_runtime
from backend.prompts import build_prompt_bundle, extract_referenced_paths
from backend.runners.base import RunnerContext
from backend.runtime_config import normalize_reasoning_effort, select_model
from backend.tools import bind_request_scoped_tools, load_local_tools
from backend.permissions import PermissionDecision


logger = logging.getLogger("k_agent.runners.k_agent")


class KAgentRunner:
    kind = "k_agent"

    async def run_stream(self, ctx: RunnerContext) -> AsyncIterator[dict[str, Any]]:
        if ctx.settings is None or ctx.mcp_pool is None or ctx.langfuse is None:
            raise RuntimeError("KAgentRunner requires settings, mcp_pool, and langfuse")

        settings = ctx.settings
        log_context = {
            "requestId": ctx.request_id or "-",
            "threadId": ctx.thread_id,
            "runId": ctx.run_id,
        }
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
            await mcp_manager.connect_all()
            model = select_model(ctx.model_id, settings)
            if ctx.attachments and not model.get("multimodal", False):
                raise ValueError("Selected model does not support image input")
            skills = ctx.skills
            selected_mcp_ids = {
                str(server.get("id"))
                for server in ctx.mcp_servers
                if server.get("id")
            }
            mcp_tools = await mcp_manager.list_tools()
            referenced_paths = extract_referenced_paths(ctx.messages)
            prompt_started_at = time.perf_counter()
            prompt_bundle = build_prompt_bundle(
                settings.system_prompt,
                skills=skills,
                referenced_paths=referenced_paths,
                mcp_tools=cast(list[Any], mcp_tools),
            )
            user_context = dict(prompt_bundle.user_context)
            if ctx.mcp_servers:
                user_context["selectedMcpServers"] = "\n".join(
                    f"- {server.get('name') or server.get('id')}: "
                    f"{server.get('description') or ''}".rstrip()
                    for server in ctx.mcp_servers
                )
            # MCP 服务返回的动态指令属于本轮上下文，不写入会话历史；
            # 下一轮会根据当时连接状态重新生成。
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
            tools = bind_request_scoped_tools(
                await load_local_tools(), mcp_manager, skills
            )
            run_request = AgentRunRequest(
                messages=ctx.messages,
                system_prompt=prompt_bundle.system_prompt,
                user_context=user_context,
                model_config=model,
                attachments=ctx.attachments,
                mcp_server_ids=selected_mcp_ids,
                reasoning_effort=normalize_reasoning_effort(
                    model, ctx.reasoning_effort
                ),
                loaded_memory_paths=prompt_bundle.memory_paths,
            )
            callbacks = [cb for cb in (ctx.logging_callback,) if cb is not None]

            async def request_approval(
                target: str,
                decision: PermissionDecision,
                detail: dict[str, Any],
            ) -> dict[str, Any]:
                if ctx.approval_broker is None:
                    raise RuntimeError("Approval broker is unavailable")
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
                )

            with ctx.langfuse.observe_agent_run(
                session_id=ctx.thread_id,
                run_id=ctx.run_id,
                model=str(model.get("model") or ctx.model_id or "unknown"),
                messages=ctx.messages,
                metadata={
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
            ) as observability_callbacks:
                agent = OpenAIAgent(
                    tools,
                    mcp_manager,
                    callbacks=[*callbacks, *observability_callbacks],
                    skills=skills,
                    approval_handler=request_approval,
                )
                async for event in agent.run_stream(run_request):
                    yield event
        finally:
            await mcp_manager.close_all()
