"""Detect which local CLI agents are installed and callable."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.runners.resolve_cli import resolve_cli
from backend.runners.cli_models import list_models_for_kind


@dataclass(frozen=True, slots=True)
class DetectedAgent:
    kind: str
    name: str
    available: bool
    command: str | None = None
    version: str | None = None
    detail: str | None = None
    # Built-in k_agent is always selectable; CLI kinds require a binary on PATH.
    requires_cli: bool = False
    # Conservative defaults exposed to the UI for session resume toggles.
    supports_resume: bool = False
    default_cli_session_mode: str = "ephemeral"
    supports_model_switch: bool = False
    default_model_id: str | None = None
    models: tuple[dict[str, str], ...] = ()


_CLI_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # kind, display name, version argv
    ("codex", "Codex", ("--version",)),
    ("claude_code", "Claude Code", ("--version",)),
)


async def detect_agents(*, version_timeout_seconds: float = 2.0) -> list[DetectedAgent]:
    """Return built-in + discovered CLI agents (always includes k_agent first)."""

    agents: list[DetectedAgent] = [
        DetectedAgent(
            kind="k_agent",
            name="K Agent",
            available=True,
            requires_cli=False,
            supports_resume=False,
            default_cli_session_mode="ephemeral",
            detail="Built-in OpenAI-compatible agent",
            supports_model_switch=True,
            # k_agent models come from /api/config/models; keep this empty.
            default_model_id=None,
            models=(),
        )
    ]
    for kind, name, version_args in _CLI_SPECS:
        agents.append(
            await _detect_cli(
                kind=kind,
                name=name,
                version_args=version_args,
                timeout_seconds=version_timeout_seconds,
            )
        )
    return agents


async def detect_agents_payload(
    *, version_timeout_seconds: float = 2.0
) -> dict[str, Any]:
    """Catalog payload for `/internal/agents` and Access Layer proxy."""

    agents = await detect_agents(version_timeout_seconds=version_timeout_seconds)
    serialized = []
    for agent in agents:
        payload = asdict(agent)
        # Frontend prefers camelCase for model fields.
        payload["supportsModelSwitch"] = payload.pop("supports_model_switch")
        payload["defaultModelId"] = payload.pop("default_model_id")
        payload["models"] = list(payload.pop("models") or [])
        serialized.append(payload)
    return {
        "defaultKind": "k_agent",
        "agents": serialized,
    }


async def _detect_cli(
    *,
    kind: str,
    name: str,
    version_args: tuple[str, ...],
    timeout_seconds: float,
) -> DetectedAgent:
    resolved = resolve_cli(kind)
    if resolved is None:
        hint = {
            "codex": "Install Codex CLI or ChatGPT app; optional K_AGENT_CODEX_PATH",
            "claude_code": "Install `claude` / `claude-internal`; optional K_AGENT_CLAUDE_PATH",
        }.get(kind, "CLI not found")
        return DetectedAgent(
            kind=kind,
            name=name,
            available=False,
            command=None,
            requires_cli=True,
            supports_resume=True,
            default_cli_session_mode="ephemeral",
            detail=hint,
            supports_model_switch=True,
            **_model_fields(kind),
        )
    command = resolved.path
    version: str | None = None
    binary_name = Path(command).name
    model_fields = _model_fields(kind)
    try:
        # Use a worker thread + subprocess.run. On Windows, uvicorn --reload
        # often runs under SelectorEventLoop, where create_subprocess_exec
        # raises NotImplementedError and would 500 /internal/agents.
        completed = await asyncio.to_thread(
            subprocess.run,
            [command, *version_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        text = (completed.stdout or "").strip()
        if not text:
            text = (completed.stderr or "").strip()
        version = text.splitlines()[0][:120] if text else None
        if completed.returncode not in (0, None) and not version:
            return DetectedAgent(
                kind=kind,
                name=name,
                available=False,
                command=command,
                version=version,
                requires_cli=True,
                supports_resume=True,
                default_cli_session_mode="ephemeral",
                detail=f"{binary_name} exited with code {completed.returncode}",
                supports_model_switch=True,
                **model_fields,
            )
    except subprocess.TimeoutExpired:
        return DetectedAgent(
            kind=kind,
            name=name,
            available=False,
            command=command,
            requires_cli=True,
            supports_resume=True,
            default_cli_session_mode="ephemeral",
            detail=f"{binary_name} --version timed out",
            supports_model_switch=True,
            **model_fields,
        )
    except OSError as exc:
        return DetectedAgent(
            kind=kind,
            name=name,
            available=False,
            command=command,
            requires_cli=True,
            supports_resume=True,
            default_cli_session_mode="ephemeral",
            detail=str(exc),
            supports_model_switch=True,
            **model_fields,
        )
    source_note = None
    if resolved.source != "which":
        source_note = f"via {resolved.source}"
    return DetectedAgent(
        kind=kind,
        name=name,
        available=True,
        command=command,
        version=version,
        requires_cli=True,
        supports_resume=True,
        default_cli_session_mode="ephemeral",
        detail=source_note,
        supports_model_switch=True,
        **model_fields,
    )


def _model_fields(kind: str) -> dict[str, Any]:
    catalog = list_models_for_kind(kind)
    models = tuple(
        {
            "id": str(item["id"]),
            "name": str(item["name"]),
            "supportsReasoning": bool(item.get("supportsReasoning", False)),
        }
        for item in catalog.get("models") or []
        if isinstance(item, dict) and item.get("id")
    )
    default_model_id = catalog.get("defaultModelId")
    if not isinstance(default_model_id, str):
        default_model_id = models[0]["id"] if models else None
    return {
        "default_model_id": default_model_id,
        "models": models,
    }
