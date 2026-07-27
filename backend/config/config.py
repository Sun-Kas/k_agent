import asyncio
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYSTEM_PROMPT = """
You are a helpful agent inside a React + Python starter project.
Use tools when they help. Keep answers concise and actionable.
If tool results are returned, ground the final answer in those results.
""".strip()


class Settings(BaseSettings):
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    port: int = Field(default=3001, alias="PORT")
    host: str = Field(default="0.0.0.0", alias="HOST")
    reload: bool = Field(default=False, alias="RELOAD")
    app_title: str = Field(default="K Agent API", alias="APP_TITLE")
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_ORIGINS")
    cors_allow_credentials: bool = Field(default=True, alias="CORS_ALLOW_CREDENTIALS")
    cors_allow_methods: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_METHODS")
    cors_allow_headers: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_HEADERS")
    mcp_config_path: str = Field(default="mcp.config.json", alias="MCP_CONFIG_PATH")
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, alias="SYSTEM_PROMPT")
    max_model_iterations: int = Field(default=6, alias="MAX_MODEL_ITERATIONS")
    stream_chunk_size: int = Field(default=24, alias="STREAM_CHUNK_SIZE")
    default_session_title: str = Field(default="新会话", alias="DEFAULT_SESSION_TITLE")
    session_title_max_length: int = Field(default=24, alias="SESSION_TITLE_MAX_LENGTH")
    status_model_started: str = Field(default="模型开始思考", alias="STATUS_MODEL_STARTED")
    tool_iteration_limit_message: str = Field(
        default="工具调用轮次达到上限，请检查工具链配置。",
        alias="TOOL_ITERATION_LIMIT_MESSAGE",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
        populate_by_name=True,
    )

_config: Optional[Settings] = None
_config_lock = asyncio.Lock()

async def get_or_init_settings() -> Settings:
    global _config
    if _config is None:
        async with _config_lock:
            if _config is None:
                _config = Settings()
    return _config
