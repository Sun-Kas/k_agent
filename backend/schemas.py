from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ChatRole = Literal["system", "user", "assistant", "tool"]


class ChatMeta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    tool_name: str | None = Field(default=None, alias="toolName")


class ChatMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    role: ChatRole
    content: str
    created_at: datetime = Field(alias="createdAt")
    meta: ChatMeta | None = None


class AgentRequest(BaseModel):
    session_id: str | None = Field(default=None, alias="sessionId")
    messages: list[ChatMessage]


class AgentResponse(BaseModel):
    session_id: str = Field(alias="sessionId")
    messages: list[ChatMessage]
    trace: list[str]


class SessionSummary(BaseModel):
    id: str
    title: str
    updated_at: datetime = Field(alias="updatedAt")
    message_count: int = Field(alias="messageCount")


class SessionState(BaseModel):
    session_id: str = Field(alias="sessionId")
    messages: list[ChatMessage]
    trace: list[str]
    tasks: list[str]
    thinking: list[dict[str, Any]] = Field(default_factory=list)


class StreamEvent(BaseModel):
    type: Literal["session", "status", "trace", "delta", "final", "error"]
    payload: dict[str, Any]


class HealthResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ok: bool
    model: str
    local_tool_count: int = Field(alias="localToolCount")
    mcp_tool_count: int = Field(alias="mcpToolCount")


class ModelProfileInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1)
    base_url: str = Field(alias="baseUrl", min_length=1)
    api_key: str | None = Field(default=None, alias="apiKey")
    api_key_env: str | None = Field(default=None, alias="apiKeyEnv")
    multimodal: bool = False
    supports_reasoning: bool = Field(default=False, alias="supportsReasoning")
    enabled: bool = True


class ModelsConfigUpdate(BaseModel):
    models: list[ModelProfileInput]


class McpServerInput(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpConfigUpdate(BaseModel):
    servers: list[McpServerInput]


class SkillInput(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(default="", max_length=20_000)
    enabled: bool = True


class SkillsConfigUpdate(BaseModel):
    skills: list[SkillInput]
