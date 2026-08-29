"""Loopback bridge from Claude Code's permission MCP tool to ApprovalBroker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import sys
from typing import Any

from backend.approvals import ApprovalBroker, consume_resume_authorization
from backend.user_questions import (
    normalize_user_question_answers,
    normalize_user_questions,
)


APPROVAL_SERVER_ID = "k_agent_human_approval"
APPROVAL_TOOL_NAME = f"mcp__{APPROVAL_SERVER_ID}__request_approval"


class ClaudeApprovalBridge:
    """Expose one run-scoped, authenticated callback for Claude print mode.

    Claude Code cannot call the in-process broker directly because its
    permission-prompt tool is a child MCP process. A random loopback endpoint
    keeps that process bidirectional without exposing a public approval API.
    """

    def __init__(
        self,
        *,
        broker: ApprovalBroker,
        thread_id: str,
        run_id: str,
        resume_authorization: dict[str, Any] | None = None,
    ) -> None:
        self._broker = broker
        self._thread_id = thread_id
        self._run_id = run_id
        self._resume_authorization = resume_authorization
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None
        self._port = 0
        # Claude's accept-for-session maps to this bridge/run lifetime only.
        self._run_allowed_tools: set[str] = set()

    async def __aenter__(self) -> "ClaudeApprovalBridge":
        self._server = await asyncio.start_server(
            self._handle_connection,
            host="127.0.0.1",
            port=0,
            limit=1_000_000,
        )
        socket = self._server.sockets[0]
        self._port = int(socket.getsockname()[1])
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def mcp_server(self) -> dict[str, Any]:
        """Return the child-only MCP configuration consumed by Claude Code."""

        return {
            "id": APPROVAL_SERVER_ID,
            "type": "stdio",
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("claude_approval_mcp.py"))],
            "env": {
                "K_AGENT_APPROVAL_HOST": "${K_AGENT_APPROVAL_HOST}",
                "K_AGENT_APPROVAL_PORT": "${K_AGENT_APPROVAL_PORT}",
                "K_AGENT_APPROVAL_TOKEN": "${K_AGENT_APPROVAL_TOKEN}",
            },
        }

    def child_env(self) -> dict[str, str]:
        """Inject routing secrets into the Claude process, never `.mcp.json`."""

        return {
            "K_AGENT_APPROVAL_HOST": "127.0.0.1",
            "K_AGENT_APPROVAL_PORT": str(self._port),
            "K_AGENT_APPROVAL_TOKEN": self._token,
        }

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        response: dict[str, Any]
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            request = json.loads(raw)
            if not secrets.compare_digest(str(request.get("token") or ""), self._token):
                raise PermissionError("invalid approval bridge token")
            tool_name = str(request.get("toolName") or "Claude Code tool")
            tool_input = request.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            if tool_name in self._run_allowed_tools:
                response = {"behavior": "allow", "updatedInput": tool_input}
            else:
                is_user_question = tool_name == "AskUserQuestion"
                questions = (
                    _normalize_claude_questions(tool_input)
                    if is_user_question
                    else None
                )
                detail = {
                    "source": "claude_permission_prompt",
                    "toolName": tool_name,
                    "input": tool_input,
                }
                if questions is not None:
                    # Access Layer validates answers from this server-owned
                    # projection; the browser never supplies question schemas.
                    detail["questions"] = questions
                decision = consume_resume_authorization(
                    self._resume_authorization,
                    title=f"Claude Code 请求调用 {tool_name}",
                    detail=detail,
                )
                if decision is None:
                    decision = await self._broker.request(
                        thread_id=self._thread_id,
                        run_id=self._run_id,
                        agent_kind="claude_code",
                        category="user_input" if is_user_question else "tool",
                        title=(
                            "Claude Code 需要你的回答"
                            if is_user_question
                            else f"Claude Code 请求调用 {tool_name}"
                        ),
                        message=(
                            "请回答问题后继续 Claude Code。"
                            if is_user_question
                            else "该工具调用需要你的确认。"
                        ),
                        detail=detail,
                    )
                if decision.get("action") == "approve":
                    if decision.get("scope") == "run":
                        self._run_allowed_tools.add(tool_name)
                    updated_input = dict(tool_input)
                    if is_user_question:
                        supplied = decision.get("answers")
                        if not isinstance(supplied, dict) or questions is None:
                            raise ValueError("Claude AskUserQuestion answers are missing")
                        updated_input.update(
                            _claude_question_updated_input(questions, supplied)
                        )
                    response = {"behavior": "allow", "updatedInput": updated_input}
                else:
                    response = {
                        "behavior": "deny",
                        "message": "用户拒绝了该工具调用。请调整方案后继续。",
                    }
        except Exception as exc:
            # A broken bridge must fail closed inside Claude instead of silently
            # turning an unavailable human check into permission.
            response = {
                "behavior": "deny",
                "message": f"Human approval unavailable: {type(exc).__name__}",
            }
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


def _normalize_claude_questions(tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Project Claude's richer question schema onto the shared HITL contract."""

    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list):
        return normalize_user_questions({"questions": raw_questions})
    projected: list[dict[str, Any]] = []
    for question in raw_questions:
        if not isinstance(question, dict):
            projected.append(question)
            continue
        options = question.get("options")
        projected_options = []
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    # Claude may include a preview field. The shared compact
                    # form currently renders label/description only.
                    projected_options.append({
                        "label": option.get("label"),
                        "description": option.get("description"),
                    })
                else:
                    projected_options.append(option)
        projected.append({
            "header": question.get("header"),
            "question": question.get("question"),
            "options": projected_options,
            "multiSelect": question.get("multiSelect", False),
        })
    return normalize_user_questions({"questions": projected})


def _claude_question_updated_input(
    questions: list[dict[str, Any]],
    supplied: dict[str, Any],
) -> dict[str, Any]:
    """Convert shared selected/custom answers to Claude's tool input shape."""

    normalized = normalize_user_question_answers(questions, supplied)
    answers: dict[str, str] = {}
    annotations: dict[str, dict[str, str]] = {}
    for question in questions:
        question_id = str(question["id"])
        answer = normalized[question_id]
        selected = answer.get("selected")
        custom = answer.get("custom")
        values = [str(value) for value in selected] if isinstance(selected, list) else []
        custom_text = custom.strip() if isinstance(custom, str) else ""
        if custom_text:
            values.append(custom_text)
            annotations[str(question["question"])] = {"notes": custom_text}
        if not values:
            raise ValueError(f"Answer for {question_id} is empty")
        answers[str(question["question"])] = ", ".join(values)
    result: dict[str, Any] = {"answers": answers}
    if annotations:
        result["annotations"] = annotations
    return result
