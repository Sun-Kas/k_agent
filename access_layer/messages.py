"""Convert AG-UI protocol messages into Access Layer ChatMessage records."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from access_layer.schemas import ChatMessage


def to_chat_messages(messages: list[Any]) -> list[ChatMessage]:
    """Keep system/user/assistant text turns; drop empty assistant leftovers."""
    converted: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content", "")
            message_id = message.get("id")
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", "")
            message_id = getattr(message, "id", None)
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        if role == "assistant" and not content.strip():
            continue
        converted.append(
            ChatMessage(
                id=str(message_id) if message_id else str(uuid.uuid4()),
                role=role,
                content=content,
                createdAt=datetime.now(timezone.utc),
            )
        )
    return converted
