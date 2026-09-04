"""Shared helpers for spawning headless CLI agents and mapping JSONL → events."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from backend.api.schemas import ChatMessage
from backend.prompts import voice_conversation_prompt
from backend.runners.base import RunnerContext


logger = logging.getLogger("k_agent.runners.cli")

CliEventMapper = Callable[[dict[str, Any], "_CliStreamState"], list[dict[str, Any]]]

# Sentinel event type: mapper returns this instead of raising so that
# run_cli_jsonl can yield any preceding events before propagating the error.
CLI_ERROR_EVENT_TYPE = "_cli_error"


class _CliStreamState:
    """Mutable mapping state shared across one CLI JSONL stream."""

    def __init__(self) -> None:
        self.message_id: str | None = None
        self.message_open = False
        self.provider_session_id: str | None = None
        self.saw_final_text = False
        # Thinking / reasoning state
        self.thinking_id: str | None = None
        self.thinking_buffer: str = ""


def build_prompt_from_messages(messages: Sequence[ChatMessage], *, max_chars: int = 900_000) -> str:
    """Flatten full Access-layer history into a single CLI prompt.

    Used in ephemeral mode where the CLI has no prior memory.
    """

    parts: list[str] = []
    for message in messages:
        content = (message.content or "").strip()
        if not content and not message.tool_calls:
            continue
        role = message.role
        if role == "tool":
            label = "tool"
        elif role == "assistant":
            label = "assistant"
        elif role == "system":
            label = "system"
        else:
            label = "user"
        if message.tool_calls:
            calls = ", ".join(
                f"{call.name}({call.arguments})" for call in message.tool_calls
            )
            content = f"{content}\n[tool_calls: {calls}]".strip()
        parts.append(f"{label}: {content}")
    prompt = "\n\n".join(parts).strip()
    if len(prompt) > max_chars:
        prompt = prompt[-max_chars:]
    return prompt or "Continue."


def extract_latest_user_prompt(messages: Sequence[ChatMessage]) -> str:
    """Return only the last user message content.

    Used in resume mode where the CLI already holds the full conversation
    history; re-sending it would cause duplication and confusion.
    """

    for message in reversed(messages):
        if message.role == "user":
            content = (message.content or "").strip()
            if content:
                return content
    return "Continue."


def build_cli_prompt(ctx: "RunnerContext") -> str:
    """Build the prompt string appropriate for the current session mode.

    - ephemeral: flatten full history (CLI has no memory)
    - resume: only the latest user message (CLI already holds context)
    """

    mode = cli_session_mode(ctx)
    prompt = (
        extract_latest_user_prompt(ctx.messages)
        if mode == "resume"
        else build_prompt_from_messages(ctx.messages)
    )
    voice_prompt = voice_conversation_prompt(ctx.options)
    if voice_prompt:
        # CLI providers have no separate per-turn system field. Prefixing the
        # transient execution prompt preserves clean Access Layer history.
        return f"[Voice conversation response style]\n{voice_prompt}\n\n{prompt}"
    return prompt


def cli_session_mode(ctx: RunnerContext) -> str:
    mode = str(ctx.options.get("cliSessionMode") or "ephemeral").strip().lower()
    return mode if mode in {"ephemeral", "resume"} else "ephemeral"


def resume_session_id(ctx: RunnerContext) -> str | None:
    value = ctx.options.get("resumeSessionId")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
_SENSITIVE_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9]{8})[A-Za-z0-9-]+"
    r"|([A-Za-z0-9_-]{20,}(?:secret|token|key|api)[A-Za-z0-9_-]*)",
    re.IGNORECASE,
)


def _sanitize_stderr(raw: str, kind: str, returncode: int) -> str:
    """Extract user-facing error lines from stderr, redacting potential secrets."""

    text = raw.strip()
    if not text:
        return f"{kind} exited with code {returncode}"
    first_line = text.splitlines()[0].strip()
    # Prefer a short first line; fall back to full text if very short.
    summary = first_line if len(first_line) > 10 else text
    summary = summary[:2000]
    summary = _SENSITIVE_PATTERN.sub(r"\1***", summary)
    return summary


async def run_cli_jsonl(
    *,
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None,
    mapper: CliEventMapper,
    kind: str,
    state_factory: Callable[[], _CliStreamState] = _CliStreamState,
) -> AsyncIterator[dict[str, Any]]:
    """Spawn a CLI, parse stdout JSONL, and yield internal agent events."""

    state = state_factory()
    logger.info("Starting %s CLI (%s)", kind, argv[0])
    # Headless runners receive the prompt through their arguments. Close stdin
    # so an open pipe cannot be mistaken for additional turn input or hang.
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    async def _drain_stderr() -> str:
        chunks: list[bytes] = []
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            text = chunk.decode("utf-8", errors="replace").strip()
            if text:
                logger.debug("%s stderr: %s", kind, text[:500])
        return b"".join(chunks).decode("utf-8", errors="replace")

    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("%s non-JSON stdout: %s", kind, text[:2000])
                continue
            if not isinstance(payload, dict):
                continue
            mapped = mapper(payload, state)
            deferred_error: str | None = None
            for event in mapped:
                if event.get("type") == CLI_ERROR_EVENT_TYPE:
                    deferred_error = str(event.get("message") or f"{kind} error")
                else:
                    yield event
                    # Yield control after each event so StreamingResponse can
                    # flush it to the client before processing the next one.
                    await asyncio.sleep(0)
            if deferred_error is not None:
                raise RuntimeError(deferred_error)
        returncode = await process.wait()
        stderr_text = await stderr_task
        if state.message_open and state.message_id:
            yield {"type": "message_end", "payload": {"messageId": state.message_id}}
            state.message_open = False
        if state.provider_session_id:
            yield {
                "type": "cli_session",
                "payload": {"kind": kind, "sessionId": state.provider_session_id},
            }
        if returncode != 0:
            detail = _sanitize_stderr(stderr_text, kind, returncode)
            raise RuntimeError(detail)
        if not state.saw_final_text and not state.message_id:
            # Some CLIs only print the final answer to a side channel; surface a
            # minimal assistant turn so Access still projects a reply.
            message_id = str(uuid.uuid4())
            yield {"type": "message_start", "payload": {"messageId": message_id}}
            yield {
                "type": "delta",
                "payload": {
                    "messageId": message_id,
                    "content": stderr_text.strip() or f"{kind} completed with no text output.",
                },
            }
            yield {"type": "message_end", "payload": {"messageId": message_id}}
        yield {
            "type": "final",
            "payload": {},
        }
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    except Exception:
        await _stop_process(process)
        raise
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """Best-effort terminate; ignore races where the child already exited."""

    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        await process.wait()
    except ProcessLookupError:
        return


def open_text_message(state: _CliStreamState) -> list[dict[str, Any]]:
    if state.message_open and state.message_id:
        return []
    state.message_id = str(uuid.uuid4())
    state.message_open = True
    return [{"type": "message_start", "payload": {"messageId": state.message_id}}]


def append_text(state: _CliStreamState, content: str) -> list[dict[str, Any]]:
    if not content:
        return []
    events = open_text_message(state)
    assert state.message_id is not None
    events.append(
        {
            "type": "delta",
            "payload": {"messageId": state.message_id, "content": content},
        }
    )
    state.saw_final_text = True
    return events


def close_text_message(state: _CliStreamState) -> list[dict[str, Any]]:
    if not state.message_open or not state.message_id:
        return []
    message_id = state.message_id
    state.message_open = False
    state.message_id = None
    return [{"type": "message_end", "payload": {"messageId": message_id}}]


def emit_thinking(state: _CliStreamState, content: str, *, finish: bool = False) -> list[dict[str, Any]]:
    """Accumulate thinking content and emit an incremental thinking event.

    The internal thinking format uses snapshot semantics (detail grows over time)
    which agui.py converts to AG-UI REASONING start/delta/end events.
    """

    if not content and not finish:
        return []
    events: list[dict[str, Any]] = []
    if state.thinking_id is None:
        state.thinking_id = str(uuid.uuid4())
        state.thinking_buffer = ""
    state.thinking_buffer += content
    status = "complete" if finish else "active"
    events.append({
        "type": "thinking",
        "payload": {
            "id": state.thinking_id,
            "phase": "reasoning",
            "title": "思考中",
            "detail": state.thinking_buffer,
            "status": status,
        },
    })
    if finish:
        state.thinking_id = None
        state.thinking_buffer = ""
    return events


def close_thinking(state: _CliStreamState) -> list[dict[str, Any]]:
    """Finalize any open thinking block before text or tool events."""

    if state.thinking_id is None:
        return []
    return emit_thinking(state, "", finish=True)


def emit_error(message: str) -> dict[str, Any]:
    """Return a sentinel event that run_cli_jsonl raises after yielding siblings."""

    return {"type": CLI_ERROR_EVENT_TYPE, "message": message}


def emit_tool_call(
    *,
    name: str,
    arguments: Any,
    result: str | None = None,
    tool_call_id: str | None = None,
) -> list[dict[str, Any]]:
    """Emit TOOL_CALL_START (and optionally TOOL_CALL_RESULT) events.

    Pass an explicit *tool_call_id* to pair a result with a previously started
    tool call when the provider exposes its own stable call identifier.
    """

    if tool_call_id is None:
        tool_call_id = str(uuid.uuid4())
    if isinstance(arguments, (dict, list)):
        args_text = json.dumps(arguments, ensure_ascii=False)
    else:
        args_text = str(arguments or "")
    events: list[dict[str, Any]] = [
        {
            "type": "tool_start",
            "payload": {
                "toolCallId": tool_call_id,
                "toolCallName": name,
                "arguments": args_text,
            },
        }
    ]
    if result is not None:
        events.append(
            {
                "type": "tool_result",
                "payload": {
                    "toolCallId": tool_call_id,
                    "messageId": str(uuid.uuid4()),
                    "content": result,
                },
            }
        )
    return events


def emit_tool_result(
    *,
    tool_call_id: str,
    content: str,
) -> list[dict[str, Any]]:
    """Emit only a TOOL_CALL_RESULT paired with an earlier tool_start."""

    return [
        {
            "type": "tool_result",
            "payload": {
                "toolCallId": tool_call_id,
                "messageId": str(uuid.uuid4()),
                "content": content,
            },
        }
    ]
