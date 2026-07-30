"""Pydantic schemas exposed by access-layer configuration and session APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ChatRole = Literal["system", "user", "assistant", "tool"]


class ChatMeta(BaseModel):
    """描述消息附加元数据，如工具名和 run ID。"""
    model_config = ConfigDict(populate_by_name=True)
    tool_name: str | None = Field(default=None, alias="toolName")
    run_id: str | None = Field(default=None, alias="runId")
    tool_call_id: str | None = Field(default=None, alias="toolCallId")


class ToolCallRecord(BaseModel):
    """One tool invocation issued by an assistant turn.

    Persisting these alongside the tool results is what lets a later turn see
    what the agent already did instead of only its final prose summary.
    """

    model_config = ConfigDict(populate_by_name=True)
    id: str
    name: str
    arguments: str = ""


class ChatMessage(BaseModel):
    """描述可写入历史并参与下一轮上下文的消息。"""
    model_config = ConfigDict(populate_by_name=True)
    id: str
    role: ChatRole
    content: str
    created_at: datetime = Field(alias="createdAt")
    meta: ChatMeta | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list, alias="toolCalls")

    def carries_context(self) -> bool:
        """Whether this message still contributes input for the next model call.

        Assistant turns that only issue tool calls have empty content but must
        survive history filtering, otherwise their paired tool results become
        orphans that providers reject.
        """

        return bool(self.content.strip()) or bool(self.tool_calls)


class SessionSummary(BaseModel):
    """描述会话列表中的轻量摘要。"""
    id: str
    title: str
    updated_at: datetime = Field(alias="updatedAt")
    message_count: int = Field(alias="messageCount")


class SessionCapabilities(BaseModel):
    """Persisted MCP and Skill selection for one conversation session."""
    model_config = ConfigDict(populate_by_name=True)
    mcp_server_ids: list[str] = Field(default_factory=list, alias="mcpServerIds")
    skill_ids: list[str] = Field(default_factory=list, alias="skillIds")


class SessionState(BaseModel):
    """描述单个会话详情和原始 AG-UI events。"""
    session_id: str = Field(alias="sessionId")
    messages: list[ChatMessage]
    trace: list[str]
    tasks: list[str]
    thinking: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: SessionCapabilities | None = None


class BashSandboxHealth(BaseModel):
    """Bash OS-sandbox readiness as reported by the Agent Backend."""

    model_config = ConfigDict(populate_by_name=True)
    available: bool
    mode: str
    command: str
    reason: str
    needs_install: bool = Field(alias="needsInstall")
    platform: str | None = None
    user_summary: str | None = Field(default=None, alias="userSummary")
    manual_install_command: str | None = Field(
        default=None, alias="manualInstallCommand"
    )
    agent_install_tool: str | None = Field(default=None, alias="agentInstallTool")


class HealthResponse(BaseModel):
    """描述 Access Layer 健康检查响应。"""
    model_config = ConfigDict(populate_by_name=True)
    ok: bool
    model: str
    local_tool_count: int = Field(alias="localToolCount")
    mcp_tool_count: int = Field(alias="mcpToolCount")
    agent_backend_ok: bool = Field(alias="agentBackendOk")
    bash_sandbox: BashSandboxHealth | None = Field(default=None, alias="bashSandbox")


class ModelProfileInput(BaseModel):
    """描述配置中心提交的模型配置项。"""
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1)
    base_url: str = Field(alias="baseUrl", min_length=1)
    api_key: str | None = Field(default=None, alias="apiKey")
    api_key_env: str | None = Field(default=None, alias="apiKeyEnv")
    multimodal: bool = False
    supports_reasoning: bool = Field(default=False, alias="supportsReasoning")
    context_window: int = Field(default=128_000, alias="contextWindow", ge=8_000)
    max_output_tokens: int = Field(default=8_192, alias="maxOutputTokens", ge=256)
    context_safety_tokens: int = Field(default=4_096, alias="contextSafetyTokens", ge=0)
    enabled: bool = True


class ModelsConfigUpdate(BaseModel):
    """描述模型配置更新请求。"""
    models: list[ModelProfileInput]


class McpServerInput(BaseModel):
    """描述配置中心提交的 MCP server 配置项。"""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, max_length=120)
    description: str = Field(default="", max_length=500)
    type: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_passthrough: list[str] = Field(default_factory=list, alias="envPassthrough")
    cwd: str | None = None
    url: str | None = None
    bearer_token_env: str | None = Field(
        default=None,
        alias="bearerTokenEnv",
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    headers: dict[str, str] = Field(default_factory=dict)
    env_headers: dict[str, str] = Field(default_factory=dict, alias="envHeaders")
    enabled: bool = True

    @field_validator(
        "command",
        "cwd",
        "url",
        "bearer_token_env",
        mode="before",
    )
    @classmethod
    def empty_optional_strings_are_none(cls, value: Any) -> Any:
        """Treat blank hidden form fields as absent before pattern validation."""

        if isinstance(value, str) and not value.strip():
            return None
        return value


class McpConfigUpdate(BaseModel):
    """描述 MCP 配置更新请求。"""
    servers: list[McpServerInput]


class SkillInput(BaseModel):
    """描述配置中心提交的 Skill 配置项。"""
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(default="", max_length=20_000)
    enabled: bool = True


class SkillsConfigUpdate(BaseModel):
    """描述 Skill 配置更新请求。"""
    skills: list[SkillInput]


class SkillCreateInput(BaseModel):
    """描述创建 Skill 的请求字段。"""
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(default="", max_length=50_000)
    paths: list[str] = Field(default_factory=list)
