"""Access Layer process settings. Extra env keys are ignored so Backend-only
variables in `.env` do not fail this process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from access_layer.home import mcp_config_path, state_dir


PROJECT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_DIR / ".env", override=False)


def _default_storage_base_dir() -> str:
    return str(state_dir())


def _default_mcp_config_path() -> str:
    return str(mcp_config_path())


class Settings(BaseSettings):
    """Public-process ports, CORS, storage, and backend URL."""

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    port: int = Field(default=3001, alias="PORT")
    host: str = Field(default="127.0.0.1", alias="HOST")
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
    default_session_title: str = Field(default="新会话", alias="DEFAULT_SESSION_TITLE")
    session_title_max_length: int = Field(default=24, alias="SESSION_TITLE_MAX_LENGTH")
    storage_backend: str = Field(default="file", alias="STORAGE_BACKEND")
    storage_base_dir: str = Field(
        default_factory=_default_storage_base_dir, alias="STORAGE_BASE_DIR"
    )
    session_storage_prefix: str = Field(default="sessions", alias="SESSION_STORAGE_PREFIX")
    server_workers: int = Field(default=1, alias="SERVER_WORKERS")
    agent_backend_log_level: str = Field(default="INFO", alias="AGENT_BACKEND_LOG_LEVEL")
    max_concurrent_agent_requests: int = Field(default=5, alias="MAX_CONCURRENT_AGENT_REQUESTS")
    request_acquire_timeout_seconds: float = Field(default=1.0, alias="REQUEST_ACQUIRE_TIMEOUT_SECONDS")
    team_runtime_enabled: bool = Field(default=True, alias="TEAM_RUNTIME_ENABLED")
    team_max_active_runs: int = Field(default=8, alias="TEAM_MAX_ACTIVE_RUNS", ge=1, le=32)
    team_task_lease_seconds: int = Field(default=120, alias="TEAM_TASK_LEASE_SECONDS", ge=30, le=3600)

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
