"""Models catalog under `$K_AGENT_HOME/config/models.json`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from access_layer.home import models_config_path
from access_layer.settings import Settings
from access_layer.storage import write_json_atomic


def models_path() -> Path:
    return models_config_path()


def load_models() -> list[dict[str, Any]]:
    path = models_config_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("models", [])


def model_api_key(model: dict[str, Any], settings: Settings) -> str | None:
    env_name = model.get("apiKeyEnv")
    if env_name:
        return os.getenv(env_name)
    return model.get("apiKey") or settings.openai_api_key


def write_models(models: list[dict[str, Any]]) -> None:
    path = models_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"models": models})
