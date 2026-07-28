from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.agent import AgentRunRequest, OpenAIAgent
from backend.api.schemas import ChatMessage
from backend.config import Settings, get_or_init_settings
from backend.mcp_tool import load_mcp_manager
from backend.tools import bind_request_scoped_tools, load_local_tools


class AgentBackendRunInput(BaseModel):
    messages: list[ChatMessage]
    api_messages: list[dict[str, Any]] = Field(alias="apiMessages")
    runtime_model_config: dict[str, Any] = Field(alias="modelConfig")
    mcp_server_ids: list[str] = Field(default_factory=list, alias="mcpServerIds")
    reasoning_effort: str | None = Field(default=None, alias="reasoningEffort")
    loaded_memory_paths: list[str] = Field(default_factory=list, alias="loadedMemoryPaths")


def create_app() -> FastAPI:
    settings = Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await get_or_init_settings()
        yield

    app = FastAPI(title=f"{settings.app_title} - Agent Backend", lifespan=lifespan)

    @app.get("/internal/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "agent-backend", "stateless": True}

    @app.post("/internal/agent/run")
    async def run_agent(payload: AgentBackendRunInput, request: Request) -> StreamingResponse:
        async def events():
            mcp_manager = await load_mcp_manager()
            await mcp_manager.connect_all()
            try:
                tools = bind_request_scoped_tools(await load_local_tools(), mcp_manager)
                agent = OpenAIAgent(tools, mcp_manager)
                run_request = AgentRunRequest(
                    messages=payload.messages,
                    api_messages=payload.api_messages,
                    model_config=payload.runtime_model_config,
                    mcp_server_ids=set(payload.mcp_server_ids),
                    reasoning_effort=payload.reasoning_effort,
                    loaded_memory_paths=payload.loaded_memory_paths,
                )
                async for event in agent.run_stream(run_request):
                    yield json.dumps(
                        jsonable_encoder(event),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n"
            finally:
                await mcp_manager.close_all()

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Request-Id": request.headers.get("x-request-id", ""),
            },
        )

    return app
