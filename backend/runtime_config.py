"""Agent-owned runtime selection for models and Skills."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.home import models_config_path
from backend.storage import write_json_atomic


def models_path() -> Path:
    """Live models catalog under `$K_AGENT_HOME/config/models.json`."""

    return models_config_path()


def load_models() -> list[dict[str, Any]]:
    """读取运行时模型配置列表。"""
    path = models_config_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("models", [])


def model_api_key(model: dict[str, Any], settings: Settings) -> str | None:
    """Resolve a model's credential from its env reference or inline value."""

    env_name = model.get("apiKeyEnv")
    if env_name:
        return os.getenv(env_name)
    return model.get("apiKey") or settings.openai_api_key


def write_models(models: list[dict[str, Any]]) -> None:
    """Persist the model catalog atomically so a crash cannot truncate it."""

    path = models_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"models": models})


def select_model(model_id: str | None, settings: Settings) -> dict[str, Any]:
    """选择请求模型或默认启用模型并注入 API key。"""
    models = load_models()
    selected = next(
        (
            model for model in models
            if model.get("id") == model_id and model.get("enabled", True)
        ),
        None,
    )
    if selected is None:
        selected = next((model for model in models if model.get("enabled", True)), None)
    if selected is None:
        raise ValueError("No enabled model is configured")
    env_name = selected.get("apiKeyEnv")
    api_key = os.getenv(env_name) if env_name else selected.get("apiKey")
    return {**selected, "apiKey": api_key or settings.openai_api_key}


def normalize_reasoning_effort(model: dict[str, Any], effort: object) -> str | None:
    """根据模型能力校验并规范化思考强度。"""
    if not model.get("supportsReasoning", False):
        return None
    value = str(effort or "none").strip().lower()
    if value == "none":
        return None
    marker = " ".join(
        str(model.get(key, ""))
        for key in ("id", "name", "model", "baseUrl", "base_url")
    ).lower()
    allowed = {"high", "max"} if "deepseek" in marker else {"low", "medium", "high"}
    return value if value in allowed else None
