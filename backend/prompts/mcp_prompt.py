"""Render the MCP capability section injected into the system prompt."""

from __future__ import annotations

from typing import Protocol


class McpPromptTool(Protocol):
    """定义可注入 MCP prompt 的工具描述协议。"""
    server_id: str
    name: str
    description: str | None


def build_mcp_dynamic_prompt(tools: list[McpPromptTool]) -> str:
    """Render connected MCP tools as an uncached dynamic prompt section."""
    if not tools:
        return ""
    by_server: dict[str, list[McpPromptTool]] = {}
    for tool in tools:
        by_server.setdefault(tool.server_id, []).append(tool)

    blocks = ["# Connected MCP Tools", "The following external tools are currently connected and may change between requests."]
    for server_id in sorted(by_server):
        blocks.append(f"## Server: {server_id}")
        for tool in sorted(by_server[server_id], key=lambda item: item.name):
            description = (tool.description or "No description provided.").strip()
            blocks.append(f"- mcp__{server_id}__{tool.name}: {description}")
    return "\n".join(blocks)
