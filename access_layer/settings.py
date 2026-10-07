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

# 注入环境变量
PROJECT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_DIR / ".env", override=False)


def _default_storage_base_dir() -> str:
    return str(state_dir())


def _default_mcp_config_path() -> str:
    return str(mcp_config_path())


class Settings(BaseSettings):
    """Public-process ports, CORS, storage, and backend URL."""


    '''
    显式传入
    ↓
    环境变量(由于继承 BaseSettings，所以会优先读取环境变量)
    ↓
    .env 文件(由于继承 BaseSettings，所以会优先读取环境变量)
    ↓
    default
    '''
    # 模型目录未单独配 apiKey 时的回退密钥；Access Layer 自身不调模型。
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    # 未指定模型时的默认对话模型 id。
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    # Access Layer HTTP 监听端口。
    port: int = Field(default=3001, alias="PORT")
    # Access Layer HTTP 监听地址。
    host: str = Field(default="127.0.0.1", alias="HOST")
    # 无状态 Agent Backend 的基址；会话执行经 HTTP 转发到这里。
    agent_backend_url: str = Field(
        default="http://127.0.0.1:3002", alias="AGENT_BACKEND_URL"
    )
    # uvicorn --reload：改代码自动重启；生产应保持 False。
    reload: bool = Field(default=False, alias="RELOAD")
    # FastAPI 应用标题（OpenAPI /docs 展示名）。
    app_title: str = Field(default="K Agent API", alias="APP_TITLE")
    # 允许跨域的前端源；默认本机 Vite 开发端口。
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
    # 托管 MCP 连接配置文件路径；默认 `$K_AGENT_HOME/config/mcp.json`。
    mcp_config_path: str = Field(
        default_factory=_default_mcp_config_path, alias="MCP_CONFIG_PATH"
    )
    # 新建会话尚未从首条消息生成标题时的占位名。
    default_session_title: str = Field(default="新会话", alias="DEFAULT_SESSION_TITLE")
    # 由首条用户消息截取会话标题时的最大字符数。
    session_title_max_length: int = Field(default=24, alias="SESSION_TITLE_MAX_LENGTH")
    # 会话/审批等持久化实现。目前仅支持 "file"（本地目录）；其他值启动时报错。
    storage_backend: str = Field(default="file", alias="STORAGE_BACKEND")
    # file 后端的根目录；默认 `$K_AGENT_HOME/state`。
    storage_base_dir: str = Field(
        default_factory=_default_storage_base_dir, alias="STORAGE_BASE_DIR"
    )
    # 相对 storage_base_dir 的会话对象前缀，实际路径为 `{prefix}/{session_id}/…`。
    session_storage_prefix: str = Field(default="sessions", alias="SESSION_STORAGE_PREFIX")
    # uvicorn worker 数。文件存储的会话 CAS 要求必须为 1；>1 会拒绝启动。
    server_workers: int = Field(default=1, alias="SERVER_WORKERS")
    # 本进程日志级别（复用 backend 的 logging 配置入口）。
    agent_backend_log_level: str = Field(default="INFO", alias="AGENT_BACKEND_LOG_LEVEL")
    # 同时进行的 agent run 全局上限（进程内信号量）。
    max_concurrent_agent_requests: int = Field(default=5, alias="MAX_CONCURRENT_AGENT_REQUESTS")
    # 拿不到全局槽或会话锁时等待多久；超时对外 429。
    request_acquire_timeout_seconds: float = Field(default=1.0, alias="REQUEST_ACQUIRE_TIMEOUT_SECONDS")
    # 是否启动 Team 调度运行时。
    team_runtime_enabled: bool = Field(default=True, alias="TEAM_RUNTIME_ENABLED")
    # Team 同时执行的任务上限（1–32）。
    team_max_active_runs: int = Field(default=8, alias="TEAM_MAX_ACTIVE_RUNS", ge=1, le=32)
    # Team 任务租约秒数；超时未续约视为可被其他 worker 回收（30–3600）。
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
