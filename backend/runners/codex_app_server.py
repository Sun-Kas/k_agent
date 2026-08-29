"""Bidirectional Codex app-server transport with human approval support."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from backend.approvals import ApprovalBroker, consume_resume_authorization
from backend.runners.cli_process import (
    _CliStreamState,
    append_text,
    close_text_message,
    close_thinking,
    emit_error,
    emit_thinking,
)


logger = logging.getLogger("k_agent.runners.codex.app_server")
CodexItemMapper = Callable[[dict[str, Any], _CliStreamState], list[dict[str, Any]]]


class CodexStreamState(_CliStreamState):
    """Codex-only state for pairing app-server item lifecycle events."""

    def __init__(self) -> None:
        super().__init__()
        self.item_tool_map: dict[str, str] = {}


async def run_codex_app_server(
    *,
    command: str,
    cwd: Path,
    prompt: str,
    model: str | None,
    effort: str | None,
    resume_thread_id: str | None,
    ephemeral: bool,
    approval_broker: ApprovalBroker,
    public_thread_id: str,
    run_id: str,
    network_access: bool,
    permission_mode: str,
    resume_authorization: dict[str, Any] | None,
    mapper: CodexItemMapper,
    env: dict[str, str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one Codex turn and bridge JSON-RPC server requests to the UI."""

    process = await asyncio.create_subprocess_exec(
        command,
        "app-server",
        "--stdio",
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
    request_id = 0
    deferred: list[dict[str, Any]] = []

    async def send(payload: dict[str, Any]) -> None:
        process.stdin.write(
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        )
        await process.stdin.drain()

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal request_id
        request_id += 1
        current_id = request_id
        await send({"id": current_id, "method": method, "params": params})
        while True:
            message = await _read_message(process)
            if message.get("id") == current_id and "method" not in message:
                if message.get("error"):
                    raise RuntimeError(_rpc_error(message["error"], method))
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            deferred.append(message)

    try:
        yield {"type": "status", "payload": {"message": "Starting codex app-server"}}
        await call(
            "initialize",
            {
                "clientInfo": {
                    "name": "k_agent",
                    "title": "K Agent",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "mcpServerOpenaiFormElicitation": True,
                },
            },
        )
        await send({"method": "initialized", "params": {}})

        full_access = permission_mode == "full_access"
        thread_params: dict[str, Any] = {
            "cwd": str(cwd),
            # App-server must be allowed to ask; the ApprovalBroker supplies the
            # bidirectional answer that codex exec cannot receive.
            "approvalPolicy": "never" if full_access else "on-request",
            "approvalsReviewer": "user",
            "sandbox": "danger-full-access" if full_access else "workspace-write",
            "model": model,
        }
        if resume_thread_id:
            thread_result = await call(
                "thread/resume",
                {**thread_params, "threadId": resume_thread_id},
            )
        else:
            thread_result = await call(
                "thread/start",
                {**thread_params, "ephemeral": ephemeral, "serviceName": "k_agent"},
            )
        thread = thread_result.get("thread") or {}
        codex_thread_id = str(thread.get("id") or thread.get("threadId") or "")
        if not codex_thread_id:
            raise RuntimeError("Codex app-server did not return a thread id")

        turn_params: dict[str, Any] = {
            "threadId": codex_thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(cwd),
            "approvalPolicy": "never" if full_access else "on-request",
            "sandboxPolicy": (
                {"type": "dangerFullAccess"}
                if full_access
                else {
                    "type": "workspaceWrite",
                    "writableRoots": [str(cwd)],
                    # Default mode keeps file isolation even when outbound
                    # access is enabled; this flag changes only the network edge.
                    "networkAccess": network_access,
                }
            ),
        }
        if model:
            turn_params["model"] = model
        normalized_effort = _normalize_effort(effort)
        if normalized_effort:
            turn_params["effort"] = normalized_effort
        await call("turn/start", turn_params)

        state = CodexStreamState()
        state.provider_session_id = codex_thread_id
        while True:
            message = deferred.pop(0) if deferred else await _read_message(process)
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}

            if method and "id" in message:
                response = await _handle_server_request(
                    method=method,
                    params=params,
                    broker=approval_broker,
                    public_thread_id=public_thread_id,
                    run_id=run_id,
                    resume_authorization=resume_authorization,
                )
                await send({"id": message["id"], "result": response})
                continue

            if method == "item/agentMessage/delta":
                for event in close_thinking(state):
                    yield event
                for event in append_text(state, str(params.get("delta") or "")):
                    yield event
                continue
            if method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
            }:
                for event in emit_thinking(state, str(params.get("delta") or "")):
                    yield event
                continue
            if method in {"item/started", "item/completed"}:
                item = params.get("item")
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_item(item)
                item_type = str(normalized.get("type") or "")
                if item_type == "agent_message" and method == "item/completed":
                    if not state.message_open:
                        for event in append_text(state, str(normalized.get("text") or "")):
                            yield event
                    for event in close_text_message(state):
                        yield event
                    continue
                for event in mapper(
                    {
                        "type": "item.started" if method.endswith("started") else "item.completed",
                        "item": normalized,
                    },
                    state,
                ):
                    yield event
                continue
            if method == "turn/started":
                yield {"type": "trace", "payload": {"entry": "codex.turn.started"}}
                continue
            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                status = str(turn.get("status") or "completed")
                error = turn.get("error")
                for event in close_thinking(state):
                    yield event
                for event in close_text_message(state):
                    yield event
                if status in {"failed", "interrupted"}:
                    yield emit_error(str(error or f"Codex turn {status}"))
                    return
                yield {
                    "type": "cli_session",
                    "payload": {"kind": "codex", "sessionId": codex_thread_id},
                }
                yield {
                    "type": "final",
                    "payload": {"messages": [], "trace": [], "tasks": [], "thinking": []},
                }
                return
            if method in {"warning", "configWarning", "deprecationNotice"}:
                yield {
                    "type": "status",
                    "payload": {"message": str(params.get("message") or method)},
                }
            elif method == "error":
                yield emit_error(str(params.get("message") or params))
                return
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    finally:
        await _stop_process(process)
        if not stderr_task.done():
            stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass


async def _handle_server_request(
    *,
    method: str,
    params: dict[str, Any],
    broker: ApprovalBroker,
    public_thread_id: str,
    run_id: str,
    resume_authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render supported Codex server requests as one generic approval card."""

    category = {
        "item/commandExecution/requestApproval": "command",
        "item/fileChange/requestApproval": "file_change",
        "item/tool/requestUserInput": "user_input",
        "mcpServer/elicitation/request": "mcp_elicitation",
        "item/permissions/requestApproval": "permissions",
    }.get(method)
    if category is None:
        raise RuntimeError(f"Unsupported Codex server request: {method}")
    title = {
        "command": "Codex 请求执行命令",
        "file_change": "Codex 请求修改文件",
        "user_input": "Codex 工具需要确认",
        "mcp_elicitation": "MCP 工具需要输入",
        "permissions": "Codex 请求额外权限",
    }[category]
    message = str(
        params.get("reason")
        or params.get("message")
        or _question_text(params)
        or "请确认是否继续该操作。"
    )
    detail = _codex_request_detail(method, params)
    decision = consume_resume_authorization(
        resume_authorization, title=title, detail=detail
    )
    if decision is None:
        decision = await broker.request(
            thread_id=public_thread_id,
            run_id=run_id,
            agent_kind="codex",
            category=category,
            title=title,
            message=message,
            detail=detail,
        )
    action = str(decision.get("action") or "cancel")
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        mapped = {
            "approve": (
                "acceptForSession"
                if decision.get("scope") == "run"
                else "accept"
            ),
            "deny": "decline",
            "cancel": "cancel",
        }[action]
        return {"decision": mapped}
    if method == "item/tool/requestUserInput":
        answers = decision.get("answers") if isinstance(decision.get("answers"), dict) else {}
        return {"answers": _tool_answers(params, action, answers)}
    if method == "mcpServer/elicitation/request":
        return {
            "action": {"approve": "accept", "deny": "decline", "cancel": "cancel"}[action],
            "content": decision.get("content") if action == "approve" else None,
        }
    requested = params.get("permissions") or params.get("requestedPermissions") or {}
    return {
        "permissions": requested if action == "approve" else {},
        "scope": "session" if decision.get("scope") == "run" else "turn",
    }


def _codex_request_detail(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a hashable provider request without binding replay-only IDs.

    Codex creates new thread/turn/item identifiers when a durable Resume must
    restart from Access Layer history. Those routing IDs cannot be part of the
    one-shot authorization hash, while the actual command, patch, permission,
    elicitation, or question payload must be. Keeping the original params at
    the top level also preserves the UI detail and question definitions.
    """

    volatile_keys = {"threadId", "turnId", "itemId"}
    arguments = {
        key: value for key, value in params.items() if key not in volatile_keys
    }
    return {
        **params,
        "method": method,
        "source": "codex_app_server",
        "toolName": method,
        "arguments": arguments,
    }


def _tool_answers(
    params: dict[str, Any],
    action: str,
    supplied: dict[str, Any],
) -> dict[str, dict[str, list[str]]]:
    """Build app-server answers, including sensible approval-button defaults."""

    result: dict[str, dict[str, list[str]]] = {}
    for question in params.get("questions") or []:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "")
        if not question_id:
            continue
        selected = supplied.get(question_id)
        if isinstance(selected, dict):
            values = selected.get("selected")
            custom = selected.get("custom")
            combined = [str(value) for value in values] if isinstance(values, list) else []
            if isinstance(custom, str) and custom.strip():
                combined.append(custom.strip())
            if combined:
                result[question_id] = {"answers": combined}
                continue
        if isinstance(selected, list) and selected:
            result[question_id] = {"answers": [str(value) for value in selected]}
            continue
        labels = [
            str(option.get("label") or "")
            for option in question.get("options") or []
            if isinstance(option, dict)
        ]
        result[question_id] = {"answers": [_select_label(labels, action)]}
    return result


def _select_label(labels: list[str], action: str) -> str:
    needles = {
        "approve": ("accept", "approve", "allow", "同意", "允许"),
        "deny": ("decline", "deny", "reject", "拒绝"),
        "cancel": ("cancel", "取消"),
    }[action]
    for label in labels:
        if any(needle in label.lower() for needle in needles):
            return label
    return labels[0] if labels else action


def _question_text(params: dict[str, Any]) -> str:
    return "\n".join(
        str(question.get("question") or "")
        for question in params.get("questions") or []
        if isinstance(question, dict) and question.get("question")
    )


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Translate app-server camelCase items to the existing Codex event mapper."""

    normalized = dict(item)
    normalized["type"] = {
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "mcpToolCall": "mcp_tool_call",
        "fileChange": "file_change",
        "webSearch": "web_search",
        "collabToolCall": "collab_tool_call",
    }.get(str(item.get("type") or ""), item.get("type"))
    for source, target in (
        ("aggregatedOutput", "aggregated_output"),
        ("exitCode", "exit_code"),
        ("commandActions", "command_actions"),
    ):
        if source in item:
            normalized[target] = item[source]
    return normalized


def _normalize_effort(effort: str | None) -> str | None:
    value = (effort or "").strip().lower()
    if value in {"", "none"}:
        return None
    return value


async def _read_message(process: asyncio.subprocess.Process) -> dict[str, Any]:
    assert process.stdout is not None
    line = await process.stdout.readline()
    if not line:
        returncode = await process.wait()
        raise RuntimeError(f"Codex app-server exited unexpectedly ({returncode})")
    try:
        payload = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex app-server returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Codex app-server returned a non-object message")
    return payload


async def _drain_stderr(stream: asyncio.StreamReader) -> str:
    chunks: list[bytes] = []
    while chunk := await stream.read(4096):
        chunks.append(chunk)
        logger.debug("Codex app-server stderr: %s", chunk.decode(errors="replace")[:500])
    return b"".join(chunks).decode("utf-8", errors="replace")


def _rpc_error(error: Any, method: str) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("data") or error)
    return f"Codex {method} failed: {error}"


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()
