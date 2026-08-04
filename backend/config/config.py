"""Environment-backed runtime settings shared by backend services."""

import asyncio
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.home import mcp_config_path, state_dir


DEFAULT_SYSTEM_PROMPT = """
You are K Agent, a helpful personal assistant for planning, answering questions, and completing tasks.
Use tools when they help. Keep answers concise, practical, and grounded in available context.
When tool results are returned, base your response on those results.
""".strip()

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
# Example templates remain in-repo; live MCP/models files live under $K_AGENT_HOME.
RUNTIME_CONFIG_DIR = BACKEND_DIR / "config" / "runtime"

# Load project-wide environment once. Existing process variables win, and every
# module can use os.getenv()/os.environ without parsing .env independently.
load_dotenv(PROJECT_DIR / ".env", override=False)


def _default_storage_base_dir() -> str:
    return str(state_dir())


def _default_mcp_config_path() -> str:
    return str(mcp_config_path())


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
    mcp_config_path: str = Field(
        default_factory=_default_mcp_config_path, alias="MCP_CONFIG_PATH"
    )
    mcp_connect_timeout_seconds: float = Field(
        default=60.0,
        alias="MCP_CONNECT_TIMEOUT_SECONDS",
        ge=1.0,
        le=300.0,
    )
    # Pooled MCP connections survive between runs so a stdio server does not pay
    # an uvx/npx cold start every turn; idle ones are dropped after this window.
    mcp_session_idle_ttl_seconds: float = Field(
        default=600.0,
        alias="MCP_SESSION_IDLE_TTL_SECONDS",
        ge=0.0,
        le=86_400.0,
    )
    mcp_call_timeout_seconds: float = Field(
        default=180.0,
        alias="MCP_CALL_TIMEOUT_SECONDS",
        ge=1.0,
        le=3600.0,
    )
    # Transient MCP failures (cancelled, timeout, connection reset) are retried
    # up to this many times before surfacing the error to the model.
    mcp_call_max_retries: int = Field(
        default=2,
        alias="MCP_CALL_MAX_RETRIES",
        ge=0,
        le=5,
    )
    mcp_call_retry_base_delay_seconds: float = Field(
        default=1.0,
        alias="MCP_CALL_RETRY_BASE_DELAY_SECONDS",
        ge=0.1,
        le=30.0,
    )
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, alias="SYSTEM_PROMPT")
    max_model_iterations: int = Field(default=1000, alias="MAX_MODEL_ITERATIONS")
    # Every provider call is bounded twice: once for establishing the request and
    # once for the gap between streamed chunks. Without both, a provider that
    # accepts the connection and then stalls holds a concurrency slot and the
    # per-session lock in the access layer forever.
    model_request_timeout_seconds: float = Field(
        default=120.0,
        alias="MODEL_REQUEST_TIMEOUT_SECONDS",
        ge=1.0,
        le=3600.0,
    )
    model_stream_idle_timeout_seconds: float = Field(
        default=90.0,
        alias="MODEL_STREAM_IDLE_TIMEOUT_SECONDS",
        ge=1.0,
        le=3600.0,
    )
    default_session_title: str = Field(default="新会话", alias="DEFAULT_SESSION_TITLE")
    session_title_max_length: int = Field(default=24, alias="SESSION_TITLE_MAX_LENGTH")
    storage_backend: str = Field(default="file", alias="STORAGE_BACKEND")
    # FileStorage root: sessions land in `$K_AGENT_HOME/state/sessions/`.
    storage_base_dir: str = Field(
        default_factory=_default_storage_base_dir, alias="STORAGE_BASE_DIR"
    )
    session_storage_prefix: str = Field(default="sessions", alias="SESSION_STORAGE_PREFIX")
    server_workers: int = Field(default=1, alias="SERVER_WORKERS")
    agent_backend_log_level: str = Field(default="INFO", alias="AGENT_BACKEND_LOG_LEVEL")
    max_concurrent_agent_requests: int = Field(default=5, alias="MAX_CONCURRENT_AGENT_REQUESTS")
    request_acquire_timeout_seconds: float = Field(default=1.0, alias="REQUEST_ACQUIRE_TIMEOUT_SECONDS")
    # Team runs have their own pool because they are background work and must
    # not hold the public conversation semaphore or same-session lock.
    team_runtime_enabled: bool = Field(default=True, alias="TEAM_RUNTIME_ENABLED")
    team_max_active_runs: int = Field(default=8, alias="TEAM_MAX_ACTIVE_RUNS", ge=1, le=32)
    team_task_lease_seconds: int = Field(default=120, alias="TEAM_TASK_LEASE_SECONDS", ge=30, le=3600)
    local_tool_preset: str = Field(default="coding", alias="LOCAL_TOOL_PRESET")
    local_tool_names: str | None = Field(default=None, alias="LOCAL_TOOL_NAMES")
    local_tool_workspace_root: str = Field(default=".", alias="LOCAL_TOOL_WORKSPACE_ROOT")
    local_tool_bash_timeout_seconds: float = Field(default=30.0, alias="LOCAL_TOOL_BASH_TIMEOUT_SECONDS")
    local_tool_max_output_chars: int = Field(default=50000, alias="LOCAL_TOOL_MAX_OUTPUT_CHARS")
    # Bash is the one tool that cannot be confined by path checks, so it is run
    # under an OS sandbox when the host provides one. `auto` degrades to an
    # unsandboxed run and says so in the tool result; `required` fails instead.
    bash_sandbox_mode: Literal["off", "auto", "required"] = Field(
        default="auto", alias="BASH_SANDBOX_MODE"
    )
    bash_sandbox_command: str = Field(default="srt", alias="BASH_SANDBOX_COMMAND")
    # Empty means no network at all: srt's network policy is allow-only.
    bash_sandbox_allowed_domains: list[str] = Field(
        default_factory=list, alias="BASH_SANDBOX_ALLOWED_DOMAINS"
    )
    bash_sandbox_write_paths: list[str] = Field(
        default_factory=list, alias="BASH_SANDBOX_WRITE_PATHS"
    )
    bash_sandbox_deny_read: list[str] = Field(
        default_factory=list, alias="BASH_SANDBOX_DENY_READ"
    )
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
