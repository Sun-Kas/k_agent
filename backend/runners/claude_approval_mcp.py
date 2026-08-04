"""Private stdio MCP tool used by Claude Code for human permission prompts."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP


server = FastMCP("K Agent Human Approval", log_level="ERROR")


@server.tool()
async def request_approval(tool_name: str, input: dict[str, Any]) -> str:
    """Suspend Claude's tool call until the Team user approves or denies it."""

    host = os.environ["K_AGENT_APPROVAL_HOST"]
    port = int(os.environ["K_AGENT_APPROVAL_PORT"])
    token = os.environ["K_AGENT_APPROVAL_TOKEN"]
    reader, writer = await asyncio.open_connection(host, port)
    writer.write((json.dumps({
        "token": token,
        "toolName": tool_name,
        "input": input,
    }, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.readline(), timeout=660)
    writer.close()
    await writer.wait_closed()
    if not raw:
        return json.dumps({
            "behavior": "deny",
            "message": "Human approval bridge closed before a decision.",
        })
    # Claude's permission-prompt MCP contract expects the decision itself as a
    # JSON-stringified text result, not an MCP structured-content object.
    return json.dumps(json.loads(raw), ensure_ascii=False)


if __name__ == "__main__":
    server.run(transport="stdio")
