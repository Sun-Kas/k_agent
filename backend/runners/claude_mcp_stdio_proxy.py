"""Claude-only bridge from authenticated remote HTTP MCP to local stdio."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Claude HTTP-to-stdio MCP bridge")
    parser.add_argument("--url", required=True)
    parser.add_argument("--bearer-token-env", default="")
    parser.add_argument("--env-headers-json", default="{}")
    parser.add_argument("--static-header-env-json", default="{}")
    return parser.parse_args()


def _headers(args: argparse.Namespace) -> dict[str, str]:
    static_header_env = json.loads(args.static_header_env_json)
    env_headers = json.loads(args.env_headers_json)
    if not isinstance(static_header_env, dict) or not isinstance(env_headers, dict):
        raise ValueError("Claude MCP bridge header configuration must be objects")
    headers: dict[str, str] = {}
    for key, env_name in {**static_header_env, **env_headers}.items():
        value = os.getenv(str(env_name))
        if not value:
            raise RuntimeError(f"Configured MCP header environment variable is not set: {env_name}")
        headers[str(key)] = value
    if args.bearer_token_env:
        token = os.getenv(args.bearer_token_env)
        if not token:
            raise RuntimeError(
                "Configured MCP Bearer token environment variable is not set: "
                f"{args.bearer_token_env}"
            )
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _tool_error_text(result: types.CallToolResult) -> str:
    parts = [
        block.text
        for block in result.content
        if isinstance(block, types.TextContent) and block.text
    ]
    return "\n".join(parts) or "Remote MCP tool returned an error"


async def _serve(args: argparse.Namespace) -> None:
    async with streamablehttp_client(args.url, headers=_headers(args) or None) as streams:
        read_stream, write_stream, _ = streams
        async with ClientSession(read_stream, write_stream) as remote:
            await remote.initialize()
            server = Server("k-agent-claude-http-mcp-bridge")

            @server.list_tools()
            async def list_tools() -> list[types.Tool]:
                return list((await remote.list_tools()).tools)

            @server.call_tool()
            async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
                result = await remote.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError(_tool_error_text(result))
                if result.structuredContent is not None:
                    return list(result.content), result.structuredContent
                return list(result.content)

            async with stdio_server() as (server_read, server_write):
                await server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                )


def main() -> None:
    try:
        asyncio.run(_serve(_arguments()))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Claude's stdio protocol owns stdout; diagnostics must remain stderr.
        print(f"Claude MCP bridge failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
