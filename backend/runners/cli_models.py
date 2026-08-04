"""Model catalogs for CLI agent kinds (Codex / Claude Code)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger("k_agent.runners.cli_models")


def list_models_for_kind(kind: str) -> dict[str, Any]:
    """Return `{defaultModelId, models:[{id,name}]}` for a runner kind."""

    if kind == "codex":
        return _codex_models()
    if kind == "claude_code":
        return _claude_models()
    return {"defaultModelId": None, "models": []}


def _codex_models() -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    cache = Path.home() / ".codex" / "models_cache.json"
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(raw_models, list):
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                visibility = str(item.get("visibility") or "list").lower()
                if visibility not in {"", "list", "visible", "default"}:
                    continue
                slug = item.get("slug") or item.get("id") or item.get("model")
                if not isinstance(slug, str) or not slug.strip():
                    continue
                name = item.get("display_name") or item.get("name") or slug
                model_id = slug.strip()
                models.append({
                    "id": model_id,
                    "name": str(name),
                    "supportsReasoning": True,
                })
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.debug("Unable to read Codex models cache: %s", exc)

    if not models:
        models = [
            {"id": "gpt-5.4", "name": "GPT-5.4", "supportsReasoning": True},
            {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini", "supportsReasoning": True},
            {"id": "o3", "name": "o3", "supportsReasoning": True},
            {"id": "o4-mini", "name": "o4-mini", "supportsReasoning": True},
        ]

    default_id = _codex_default_model()
    if default_id and not any(model["id"] == default_id for model in models):
        models.insert(0, {
            "id": default_id,
            "name": default_id,
            "supportsReasoning": True,
        })
    if not default_id:
        default_id = models[0]["id"]
    return {"defaultModelId": default_id, "models": models}


def _codex_default_model() -> str | None:
    """Read only the `model` key from ~/.codex/config.toml when present."""

    path = Path.home() / ".codex" / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != "model":
            continue
        raw = value.strip().strip('"').strip("'")
        return raw or None
    return None


def _claude_models() -> dict[str, Any]:
    # Claude Code accepts aliases (`sonnet`/`opus`/`haiku`) or full model names.
    # All current Claude models support extended thinking via --thinking-budget-tokens.
    models = [
        {"id": "sonnet", "name": "Sonnet（默认别名）", "supportsReasoning": True},
        {"id": "opus", "name": "Opus", "supportsReasoning": True},
        {"id": "haiku", "name": "Haiku", "supportsReasoning": True},
        {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "supportsReasoning": True},
        {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5", "supportsReasoning": True},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "supportsReasoning": True},
    ]
    return {"defaultModelId": "sonnet", "models": models}
