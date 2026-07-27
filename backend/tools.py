from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ToolExecutor = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: ToolExecutor


async def get_current_time(_: dict[str, Any]) -> str:
    return json.dumps({"now": datetime.now(timezone.utc).isoformat()})


async def echo_text(payload: dict[str, Any]) -> str:
    return json.dumps({"echoed": str(payload.get("text", ""))})


LOCAL_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_current_time",
        description="Get the current local server time in ISO format.",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        execute=get_current_time,
    ),
    ToolDefinition(
        name="echo_text",
        description="Echo user-provided text for testing the tool pipeline.",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to echo back",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        execute=echo_text,
    ),
]
