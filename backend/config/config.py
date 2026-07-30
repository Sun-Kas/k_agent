"""Environment-backed runtime settings shared by backend services."""

import asyncio
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYSTEM_PROMPT = """
You are K Agent, a helpful personal assistant for planning, answering questions, and completing tasks.
Use tools when they help. Keep answers concise, practical, and grounded in available context.
When tool results are returned, base your response on those results.
""".strip()

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
RUNTIME_CONFIG_DIR = BACKEND_DIR / "config" / "runtime"

# Load project-wide environment once. Existing process variables win, and every
# module can use os.getenv()/os.environ without parsing .env independently.
load_dotenv(PROJECT_DIR / ".env", override=False)


class Settings(BaseSettings):
    """集中定义服务端端口、模型默认值、存储路径和工具限制。"""
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    port: int = Field(default=3001, alias="PORT")
    # Public APIs stay on the loopback interface by default so a local
    # deployment is not accidentally reachable from the surrounding network.
    host: str = Field(default="127.0.0.1", alias="HOST")
    agent_backend_host: str = Field(default="127.0.0.1", alias="AGENT_BACKEND_HOST")
    agent_backend_port: int = Field(default=3002, alias="AGENT_BACKEND_PORT")
    agent_backend_url: str = Field(
        default="http://127.0.0.1:3002", alias="AGENT_BACKEND_URL"
    )
    reload: bool = Field(default=False, alias="RELOAD")
    app_title: str = Field(default="K Agent API", alias="APP_TITLE")
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        alias="CORS_ALLOW_ORIGINS",
    )
    cors_allow_credentials: bool = Field(default=True, alias="CORS_ALLOW_CREDENTIALS")
    cors_allow_methods: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_METHODS")
    cors_allow_headers: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_HEADERS")
    mcp_config_path: str = Field(default=str(RUNTIME_CONFIG_DIR / "mcp.config.json"), alias="MCP_CONFIG_PATH")
    mcp_connect_timeout_seconds: float = Field(
        default=60.0,
        alias="MCP_CONNECT_TIMEOUT_SECONDS",
        ge=1.0,
        le=300.0,
    )
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, alias="SYSTEM_PROMPT")
    max_model_iterations: int = Field(default=1000, alias="MAX_MODEL_ITERATIONS")
    stream_chunk_size: int = Field(default=24, alias="STREAM_CHUNK_SIZE")
    default_session_title: str = Field(default="新会话", alias="DEFAULT_SESSION_TITLE")
    session_title_max_length: int = Field(default=24, alias="SESSION_TITLE_MAX_LENGTH")
    storage_backend: str = Field(default="file", alias="STORAGE_BACKEND")
    storage_base_dir: str = Field(default=str(PROJECT_DIR / "data"), alias="STORAGE_BASE_DIR")
    session_storage_prefix: str = Field(default="sessions", alias="SESSION_STORAGE_PREFIX")
    server_workers: int = Field(default=1, alias="SERVER_WORKERS")
    agent_backend_log_level: str = Field(default="INFO", alias="AGENT_BACKEND_LOG_LEVEL")
    max_concurrent_agent_requests: int = Field(default=5, alias="MAX_CONCURRENT_AGENT_REQUESTS")
    request_acquire_timeout_seconds: float = Field(default=1.0, alias="REQUEST_ACQUIRE_TIMEOUT_SECONDS")
    local_tool_preset: str = Field(default="coding", alias="LOCAL_TOOL_PRESET")
    local_tool_names: str | None = Field(default=None, alias="LOCAL_TOOL_NAMES")
    local_tool_workspace_root: str = Field(default=".", alias="LOCAL_TOOL_WORKSPACE_ROOT")
    local_tool_bash_timeout_seconds: float = Field(default=30.0, alias="LOCAL_TOOL_BASH_TIMEOUT_SECONDS")
    local_tool_max_output_chars: int = Field(default=50000, alias="LOCAL_TOOL_MAX_OUTPUT_CHARS")
    status_model_started: str = Field(default="模型开始思考", alias="STATUS_MODEL_STARTED")
    tool_iteration_limit_message: str = Field(
        default="工具调用轮次达到上限，请检查工具链配置。",
        alias="TOOL_ITERATION_LIMIT_MESSAGE",
    )
    langfuse_enabled: bool = Field(default=True, alias="LANGFUSE_ENABLED")
    langfuse_public_key: str | None = Field(
        default=None,
        alias="LANGFUSE_PUBLIC_KEY",
        repr=False,
    )
    langfuse_secret_key: str | None = Field(
        default=None,
        alias="LANGFUSE_SECRET_KEY",
        repr=False,
    )
    langfuse_base_url: str = Field(
        default="https://cloud.langfuse.com",
        alias="LANGFUSE_BASE_URL",
    )
    langfuse_environment: str = Field(
        default="development",
        alias="LANGFUSE_TRACING_ENVIRONMENT",
    )
    langfuse_release: str | None = Field(default=None, alias="LANGFUSE_RELEASE")
    langfuse_sample_rate: float = Field(
        default=1.0,
        alias="LANGFUSE_SAMPLE_RATE",
        ge=0.0,
        le=1.0,
    )
    langfuse_timeout_seconds: int = Field(
        default=5,
        alias="LANGFUSE_TIMEOUT_SECONDS",
        ge=1,
        le=60,
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
    """返回当前应用 Settings 实例。"""
    global _config
    if _config is None:
        async with _config_lock:
            if _config is None:
                _config = Settings()
    return _config
