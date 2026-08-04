"""Claude Code-specific MCP configuration and credential isolation."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


logger = logging.getLogger("k_agent.runners.claude.mcp")


def write_claude_mcp_config(
    workspace: Path, mcp_servers: Sequence[dict[str, Any]]
) -> Path | None:
    """Write only the MCP shape understood by Claude Code."""

    if not mcp_servers:
        return None
    servers: dict[str, dict[str, Any]] = {}
    for server in mcp_servers:
        server_id = str(server.get("id") or "").strip()
        if not server_id:
            continue
        transport = str(server.get("type") or "stdio")
        entry: dict[str, Any] = {}
        if transport in {"http", "sse"}:
            url = server.get("url")
            if not url:
                continue
            bearer_env = str(server.get("bearerTokenEnv") or "").strip()
            env_headers = {
                str(key): str(value)
                for key, value in (server.get("envHeaders") or {}).items()
                if value
            }
            if bearer_env or env_headers:
                # This bridge is deliberately Claude-only. Codex uses app-server
                # HTTP MCP support and K Agent uses its native MCP manager.
                static_header_env = {
                    str(key): _proxy_header_env_name(server_id, str(key))
                    for key in (server.get("headers") or {})
                }
                entry["command"] = sys.executable
                entry["args"] = [
                    str(Path(__file__).with_name("claude_mcp_stdio_proxy.py")),
                    "--url",
                    str(url),
                    "--bearer-token-env",
                    bearer_env,
                    "--env-headers-json",
                    json.dumps(env_headers, separators=(",", ":")),
                    "--static-header-env-json",
                    json.dumps(static_header_env, separators=(",", ":")),
                ]
                dynamic_names = set(env_headers.values())
                dynamic_names.update(static_header_env.values())
                if bearer_env:
                    dynamic_names.add(bearer_env)
                entry["env"] = {
                    name: f"${{{name}}}" for name in sorted(dynamic_names)
                }
                servers[server_id] = entry
                continue
            entry["type"] = "http"
            entry["url"] = url
            headers = {
                str(key): str(value)
                for key, value in (server.get("headers") or {}).items()
            }
            if headers:
                entry["headers"] = headers
        else:
            command = server.get("command")
            if not command:
                continue
            entry["command"] = command
            if server.get("args"):
                entry["args"] = server["args"]
            server_env = {
                str(key): str(value)
                for key, value in (server.get("env") or {}).items()
            }
            for env_name in server.get("envPassthrough") or []:
                name = str(env_name).strip()
                if name:
                    server_env[name] = f"${{{name}}}"
            if server_env:
                entry["env"] = server_env
            if server.get("cwd"):
                entry["cwd"] = server["cwd"]
        servers[server_id] = entry
    if not servers:
        return None
    config_path = workspace / ".mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.debug(
        "Wrote Claude MCP config with %d server(s) to %s",
        len(servers),
        config_path,
    )
    return config_path


def inject_claude_mcp_secrets(
    env: dict[str, str], mcp_servers: Sequence[dict[str, Any]]
) -> None:
    """Put Claude bridge headers in child-only environment slots."""

    for server in mcp_servers:
        if str(server.get("type") or "stdio") not in {"http", "sse"}:
            continue
        server_id = str(server.get("id") or "").strip()
        for key, value in (server.get("headers") or {}).items():
            env[_proxy_header_env_name(server_id, str(key))] = str(value)


def _proxy_header_env_name(server_id: str, header: str) -> str:
    digest = hashlib.sha256(f"{server_id}\0{header}".encode()).hexdigest()[:16].upper()
    return f"K_AGENT_CLAUDE_MCP_HEADER_{digest}"
