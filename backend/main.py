from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from ag_ui.core import EventType, RunAgentInput
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from backend.agent import OpenAIAgent
from backend.agui import encode_event, to_chat_messages, translate_agent_events
from backend.config import Settings, get_or_init_settings
from backend.mcp_tool import McpClientManager, load_mcp_servers
from backend.schemas import (
    AgentRequest,
    AgentResponse,
    ChatMessage,
    HealthResponse,
    McpConfigUpdate,
    ModelsConfigUpdate,
    SessionState,
    SkillsConfigUpdate,
)
from backend.session_store import SessionStore
from backend.tools import LOCAL_TOOLS


SKILLS_CONFIG_PATH = Path("skills.config.json")
MODELS_CONFIG_PATH = Path("models.config.json")


def _load_skills() -> list[dict]:
    if not SKILLS_CONFIG_PATH.exists():
        return []
    payload = json.loads(SKILLS_CONFIG_PATH.read_text(encoding="utf-8"))
    return payload.get("skills", [])


def _load_models(settings: Settings) -> list[dict]:
    if not MODELS_CONFIG_PATH.exists():
        return []
    return json.loads(MODELS_CONFIG_PATH.read_text(encoding="utf-8")).get("models", [])


def _model_api_key(model: dict, settings: Settings) -> str | None:
    env_name = model.get("apiKeyEnv")
    if env_name:
        return os.getenv(env_name)
    return model.get("apiKey") or settings.openai_api_key


def _public_models(settings: Settings) -> list[dict]:
    return [{
        **model,
        "apiKey": None,
        "apiKeyConfigured": bool(_model_api_key(model, settings)),
    } for model in _load_models(settings)]


def _compose_system_prompt(base_prompt: str, skills: list[dict]) -> str:
    enabled = [skill for skill in skills if skill.get("enabled") and skill.get("instructions", "").strip()]
    if not enabled:
        return base_prompt
    blocks = "\n\n".join(
        f"## Skill: {skill['name']}\n{skill['instructions'].strip()}" for skill in enabled
    )
    return f"{base_prompt.rstrip()}\n\n# Enabled skills\n\n{blocks}"


def create_app() -> FastAPI:
    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = await get_or_init_settings()
        app.state.base_system_prompt = app.state.settings.system_prompt
        mcp_manager = McpClientManager(await load_mcp_servers())
        await mcp_manager.connect_all()
        app.state.mcp_manager = mcp_manager
        app.state.agent = OpenAIAgent(LOCAL_TOOLS, mcp_manager)
        app.state.agent.config.system_prompt = _compose_system_prompt(
            app.state.base_system_prompt, _load_skills()
        )
        app.state.session_store = SessionStore()
        yield
        await mcp_manager.close_all()

    app = FastAPI(title=settings.app_title, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    @app.get("/api/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        mcp_tools = await app.state.mcp_manager.list_tools()
        return HealthResponse(
            ok=True,
            model=app.state.settings.openai_model,
            localToolCount=len(LOCAL_TOOLS),
            mcpToolCount=len(mcp_tools),
        )

    @app.get("/api/config/models")
    async def get_models_config():
        return {
            "path": str(MODELS_CONFIG_PATH),
            "source": str(MODELS_CONFIG_PATH),
            "models": _public_models(app.state.settings),
        }

    @app.put("/api/config/models")
    async def update_models_config(payload: ModelsConfigUpdate):
        previous = {model["id"]: model for model in _load_models(app.state.settings)}
        models = []
        for profile in payload.models:
            model = profile.model_dump(by_alias=True)
            if not model.get("apiKey"):
                model["apiKey"] = previous.get(profile.id, {}).get("apiKey")
            models.append(model)
        MODELS_CONFIG_PATH.write_text(
            json.dumps({"models": models}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {"ok": True, "models": _public_models(app.state.settings)}

    @app.get("/api/config/mcp")
    async def get_mcp_config():
        settings = app.state.settings
        path = Path(settings.mcp_config_path)
        source_path = path
        is_template = False
        if not source_path.exists():
            example_path = Path("mcp.config.example.json")
            if example_path.exists():
                source_path = example_path
                is_template = True
        raw_servers = []
        if source_path.exists():
            raw_servers = json.loads(source_path.read_text(encoding="utf-8")).get("servers", [])
        connected = set(app.state.mcp_manager.sessions)
        servers = []
        for server in raw_servers:
            servers.append({
                **server,
                "enabled": server.get("enabled", True),
                "env": {key: "***" for key in server.get("env", {})},
                "connected": server.get("id") in connected,
            })
        return {
            "path": str(path),
            "source": str(source_path),
            "isTemplate": is_template,
            "servers": servers,
        }

    @app.put("/api/config/mcp")
    async def update_mcp_config(payload: McpConfigUpdate):
        path = Path(app.state.settings.mcp_config_path)
        previous = {}
        if path.exists():
            for server in json.loads(path.read_text(encoding="utf-8")).get("servers", []):
                previous[server.get("id")] = server
        servers = []
        for server in payload.servers:
            item = server.model_dump()
            old_env = previous.get(server.id, {}).get("env", {})
            item["env"] = {
                key: old_env.get(key, value) if value == "***" else value
                for key, value in item["env"].items()
            }
            servers.append(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"servers": servers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, "restartRequired": True}

    @app.get("/api/config/skills")
    async def get_skills_config():
        return {"path": str(SKILLS_CONFIG_PATH), "skills": _load_skills()}

    @app.put("/api/config/skills")
    async def update_skills_config(payload: SkillsConfigUpdate):
        skills = [skill.model_dump() for skill in payload.skills]
        SKILLS_CONFIG_PATH.write_text(
            json.dumps({"skills": skills}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        app.state.agent.config.system_prompt = _compose_system_prompt(
            app.state.base_system_prompt, skills
        )
        return {"ok": True, "enabledCount": sum(skill["enabled"] for skill in skills)}

    @app.post("/api/agent/respond", response_model=AgentResponse)
    async def respond(payload: AgentRequest) -> AgentResponse:
        try:
            session = await app.state.session_store.get_or_create(payload.session_id)
            result = await app.state.agent.run(payload.messages)
            await app.state.session_store.update(
                session.id, result["messages"], result["trace"], result["tasks"], result["thinking"]
            )
            return AgentResponse(sessionId=session.id, **result)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/agent")
    async def run_agui_agent(payload: RunAgentInput) -> StreamingResponse:
        session = await app.state.session_store.get_or_create(payload.thread_id)
        messages = to_chat_messages(payload.messages)
        forwarded = payload.forwarded_props or {}
        model_id = forwarded.get("modelId")
        model = next(
            (item for item in _load_models(app.state.settings) if item["id"] == model_id and item.get("enabled", True)),
            None,
        )
        if model is None:
            model = next((item for item in _load_models(app.state.settings) if item.get("enabled", True)), None)
        if model is None:
            raise HTTPException(status_code=400, detail="No enabled model is configured")
        model = {**model, "apiKey": _model_api_key(model, app.state.settings)}
        attachments = forwarded.get("attachments", [])
        if attachments and not model.get("multimodal", False):
            raise HTTPException(status_code=400, detail="Selected model does not support image input")
        reasoning_effort = forwarded.get("reasoningEffort")
        if not model.get("supportsReasoning", False):
            reasoning_effort = None
        selected_skill_ids = set(forwarded.get("skillIds", []))
        selected_skills = [
            skill for skill in _load_skills()
            if skill.get("enabled", True) and skill.get("id") in selected_skill_ids
        ]
        system_prompt = _compose_system_prompt(app.state.base_system_prompt, selected_skills)

        async def event_generator():
            events = translate_agent_events(
                app.state.agent.run_stream(
                    messages,
                    model_config=model,
                    mcp_server_ids=set(forwarded.get("mcpServerIds", [])),
                    system_prompt=system_prompt,
                    reasoning_effort=reasoning_effort,
                    attachments=attachments,
                ),
                thread_id=session.id,
                run_id=payload.run_id,
            )
            async for event in events:
                if event.type == EventType.STATE_SNAPSHOT:
                    snapshot = event.snapshot
                    await app.state.session_store.update(
                        session.id,
                        [ChatMessage.model_validate(item) for item in snapshot["messages"]],
                        snapshot["trace"],
                        snapshot["tasks"],
                        snapshot.get("thinking", []),
                    )
                yield encode_event(event)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/sessions")
    async def list_sessions():
        return app.state.session_store.list_summaries()

    @app.get("/api/sessions/{session_id}", response_model=SessionState)
    async def get_session(session_id: str) -> SessionState:
        session = app.state.session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionState(
            sessionId=session.id,
            messages=session.messages,
            trace=session.trace,
            tasks=session.tasks,
            thinking=session.thinking,
        )

    return app
