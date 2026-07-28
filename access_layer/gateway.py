from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from ag_ui.core import EventType, RunAgentInput
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from access_layer.agent_backend_client import AgentBackendClient
from access_layer.agui import encode_event, to_chat_messages, translate_agent_events
from access_layer.concurrency import ConcurrencyLimitExceeded, RequestConcurrencyLimiter
from access_layer.mcp_instructions import MCP_INSTRUCTIONS_DELTAS
from access_layer.request_context import (
    get_request_context,
    reset_request_context,
    set_request_context,
    update_request_context,
)
from access_layer.session_memory import MEMORY_SESSIONS
from access_layer.sessions.store import SessionRecord, SessionStore
from backend.api.schemas import ChatMessage
from backend.mcp_tool import McpClientManager
from backend.prompts import (
    build_prompt_bundle,
    extract_referenced_paths,
    prepend_user_context,
)
from backend.skills import SkillDefinition, mcp_prompt_to_skill


ModelsLoader = Callable[[], list[dict[str, Any]]]
ApiKeyResolver = Callable[[dict[str, Any]], str | None]
SkillsLoader = Callable[[], list[SkillDefinition]]
ReasoningNormalizer = Callable[[dict[str, Any], object], str | None]
McpManagerProvider = Callable[[], McpClientManager]


class AgentAccessLayer:
    """Frontend ingress and anti-corruption layer for the agent backend."""

    def __init__(
        self,
        *,
        base_system_prompt: str,
        session_store: SessionStore,
        request_limiter: RequestConcurrencyLimiter,
        agent_backend_client: AgentBackendClient,
        mcp_manager_provider: McpManagerProvider,
        load_models: ModelsLoader,
        resolve_api_key: ApiKeyResolver,
        load_skills: SkillsLoader,
        normalize_reasoning_effort: ReasoningNormalizer,
    ) -> None:
        self._base_system_prompt = base_system_prompt
        self._session_store = session_store
        self._request_limiter = request_limiter
        self._agent_backend_client = agent_backend_client
        self._mcp_manager_provider = mcp_manager_provider
        self._load_models = load_models
        self._resolve_api_key = resolve_api_key
        self._load_skills = load_skills
        self._normalize_reasoning_effort = normalize_reasoning_effort

    async def run(self, payload: RunAgentInput) -> StreamingResponse:
        messages = to_chat_messages(payload.messages)
        forwarded = payload.forwarded_props or {}
        model = self._select_model(forwarded.get("modelId"))
        attachments = self._validate_attachments(model, forwarded.get("attachments", []))
        selected_skills = self._validate_and_select_skills(forwarded.get("skillIds", []))
        reasoning_effort = self._normalize_reasoning_effort(
            model, forwarded.get("reasoningEffort")
        )
        session = await self._session_store.get_or_create(payload.thread_id)
        context_token = update_request_context(session_id=session.id, run_id=payload.run_id)
        try:
            stream_request_context = get_request_context()
            stream_guard = self._request_limiter.protect(session.id)
            try:
                await stream_guard.__aenter__()
            except ConcurrencyLimitExceeded as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc

            async def event_generator():
                stream_context_token = (
                    set_request_context(stream_request_context)
                    if stream_request_context is not None
                    else update_request_context(session_id=session.id, run_id=payload.run_id)
                )
                try:
                    async for event in self._stream_agent_request(
                        session=session,
                        run_id=payload.run_id,
                        messages=messages,
                        model=model,
                        attachments=attachments,
                        forwarded=forwarded,
                        selected_skills=selected_skills,
                        reasoning_effort=reasoning_effort,
                    ):
                        if event.type == EventType.STATE_SNAPSHOT:
                            await self._persist_snapshot(session.id, event.snapshot)
                        yield encode_event(event)
                finally:
                    await stream_guard.__aexit__(None, None, None)
                    reset_request_context(stream_context_token)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        finally:
            reset_request_context(context_token)

    async def _stream_agent_request(
        self,
        *,
        session: SessionRecord,
        run_id: str,
        messages: list[ChatMessage],
        model: dict[str, Any],
        attachments: list[dict[str, Any]],
        forwarded: dict[str, Any],
        selected_skills: list[SkillDefinition],
        reasoning_effort: str | None,
    ):
        mcp_manager = self._mcp_manager_provider()
        selected_mcp_ids = set(forwarded.get("mcpServerIds", []))
        mcp_tools = await mcp_manager.list_tools()
        if selected_mcp_ids:
            mcp_tools = [tool for tool in mcp_tools if tool.server_id in selected_mcp_ids]
        mcp_prompts = await mcp_manager.list_prompts()
        if selected_mcp_ids:
            mcp_prompts = {
                key: value for key, value in mcp_prompts.items() if key in selected_mcp_ids
            }
        prompt_bundle = build_prompt_bundle(
            self._base_system_prompt,
            skills=[
                *selected_skills,
                *[
                    mcp_prompt_to_skill(server_id, prompt)
                    for server_id, prompts in mcp_prompts.items()
                    for prompt in prompts
                ],
            ],
            referenced_paths=[
                *extract_referenced_paths(messages),
                *MEMORY_SESSIONS.get(session.id).consume_triggers(),
            ],
            mcp_tools=cast(list[Any], mcp_tools),
        )
        MEMORY_SESSIONS.get(session.id).mark_loaded(prompt_bundle.memory_paths)
        user_context = self._apply_mcp_instruction_delta(
            prompt_bundle.user_context, session.id, mcp_manager
        )
        request_context = get_request_context()
        request_id = request_context.request_id if request_context else "-"
        backend_events = self._agent_backend_client.stream(
            {
                "messages": [
                    message.model_dump(by_alias=True, mode="json")
                    for message in messages
                ],
                "apiMessages": self._build_api_messages(
                    messages,
                    prompt_bundle.system_prompt,
                    user_context,
                    attachments,
                ),
                "modelConfig": model,
                "mcpServerIds": sorted(selected_mcp_ids),
                "reasoningEffort": reasoning_effort,
                "loadedMemoryPaths": prompt_bundle.memory_paths,
            },
            request_id,
        )
        events = translate_agent_events(
            self._enrich_agent_events(
                backend_events,
                request_id=request_id,
                session_id=session.id,
                run_id=run_id,
            ),
            thread_id=session.id,
            run_id=run_id,
            previous_messages=session.messages,
        )
        async for event in events:
            yield event

    def _select_model(self, model_id: object) -> dict[str, Any]:
        models = self._load_models()
        model = next(
            (item for item in models if item["id"] == model_id and item.get("enabled", True)),
            None,
        )
        if model is None:
            model = next((item for item in models if item.get("enabled", True)), None)
        if model is None:
            raise HTTPException(status_code=400, detail="No enabled model is configured")
        return {**model, "apiKey": self._resolve_api_key(model)}

    @staticmethod
    def _validate_attachments(
        model: dict[str, Any], attachments: object
    ) -> list[dict[str, Any]]:
        if not isinstance(attachments, list):
            raise HTTPException(status_code=400, detail="attachments must be a list")
        if attachments and not model.get("multimodal", False):
            raise HTTPException(
                status_code=400, detail="Selected model does not support image input"
            )
        return [item for item in attachments if isinstance(item, dict)]

    def _validate_and_select_skills(self, requested: object) -> list[SkillDefinition]:
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
            raise HTTPException(status_code=400, detail="skillIds must be a list of strings")
        available = [skill for skill in self._load_skills() if not skill.disable_model_invocation]
        if not requested:
            return available
        by_identifier = {
            identifier: skill
            for skill in available
            for identifier in (skill.id, skill.name)
        }
        unknown = sorted(set(requested) - set(by_identifier))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown or disabled skills: {', '.join(unknown)}",
            )
        selected: list[SkillDefinition] = []
        selected_ids: set[str] = set()
        for item in requested:
            skill = by_identifier[item]
            if skill.id not in selected_ids:
                selected.append(skill)
                selected_ids.add(skill.id)
        return selected

    @staticmethod
    def _build_api_messages(
        messages: list[ChatMessage],
        system_prompt: str,
        user_context: dict[str, str],
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        body: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            content: Any = message.content
            if attachments and index == len(messages) - 1 and message.role == "user":
                content = [{"type": "text", "text": message.content}]
                content.extend(
                    {
                        "type": "image_url",
                        "image_url": {"url": attachment["dataUrl"]},
                    }
                    for attachment in attachments
                    if attachment.get("dataUrl")
                )
            body.append({"role": message.role, "content": content})
        return [
            {"role": "system", "content": system_prompt},
            *prepend_user_context(body, user_context),
        ]

    @staticmethod
    def _apply_mcp_instruction_delta(
        user_context: dict[str, str],
        session_id: str,
        mcp_manager: McpClientManager,
    ) -> dict[str, str]:
        delta = MCP_INSTRUCTIONS_DELTAS.delta(
            session_id, mcp_manager.connected_instructions()
        )
        if not delta["addedBlocks"] and not delta["removedNames"]:
            return user_context
        rendered = []
        if delta["addedBlocks"]:
            rendered.append(
                "The following MCP server instructions became available:\n\n"
                + "\n\n".join(delta["addedBlocks"])
            )
        if delta["removedNames"]:
            rendered.append(
                "The following MCP servers are no longer connected: "
                + ", ".join(delta["removedNames"])
            )
        return {**user_context, "mcpInstructionsDelta": "\n\n".join(rendered)}

    async def _persist_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        await self._session_store.update(
            session_id,
            [ChatMessage.model_validate(item) for item in snapshot["messages"]],
            snapshot["trace"],
            snapshot["tasks"],
            snapshot.get("thinking", []),
            snapshot.get("thinkingGroups", []),
        )

    @staticmethod
    async def _enrich_agent_events(
        events: AsyncIterator[dict[str, Any]],
        *,
        request_id: str,
        session_id: str,
        run_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Add access/UI state that is unrelated to the model/tool runtime."""
        request_trace = f"request:{request_id}:session:{session_id}:run:{run_id}"
        yield {
            "type": "trace",
            "payload": {"entry": request_trace},
        }
        async for event in events:
            if event.get("type") == "final":
                payload = event["payload"]
                event = {
                    **event,
                    "payload": {
                        **payload,
                        "trace": [request_trace, *payload["trace"]],
                        "tasks": AgentAccessLayer._extract_tasks(payload["messages"]),
                    },
                }
            yield event

    @staticmethod
    def _extract_tasks(messages: list[ChatMessage]) -> list[str]:
        for message in reversed(messages):
            if message.role != "user":
                continue
            lines = [line.strip("- ").strip() for line in message.content.splitlines()]
            tasks = [line for line in lines if line][:4]
            if tasks:
                return tasks
        return []
