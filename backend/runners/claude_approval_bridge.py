"""Loopback bridge from Claude Code's permission MCP tool to ApprovalBroker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import sys
from typing import Any

from backend.approvals import ApprovalBroker


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
    ) -> None:
        self._broker = broker
        self._thread_id = thread_id
        self._run_id = run_id
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None
        self._port = 0
        self._remembered_tools: set[str] = set()

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
            if tool_name in self._remembered_tools:
                response = {"behavior": "allow", "updatedInput": tool_input}
            else:
                decision = await self._broker.request(
                    thread_id=self._thread_id,
                    run_id=self._run_id,
                    agent_kind="claude_code",
                    category="tool",
                    title=f"Claude Code 请求调用 {tool_name}",
                    message="该工具调用需要你的确认。",
                    detail={
                        "source": "claude_permission_prompt",
                        "toolName": tool_name,
                        "input": tool_input,
                    },
                )
                if decision.get("action") == "approve":
                    if decision.get("remember"):
                        self._remembered_tools.add(tool_name)
                    response = {"behavior": "allow", "updatedInput": tool_input}
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
