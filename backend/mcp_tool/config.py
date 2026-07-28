from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class McpScope(str, Enum):
    LOCAL = "local"
    USER = "user"
    PROJECT = "project"
    DYNAMIC = "dynamic"
    MANAGED = "managed"
    PLUGIN = "plugin"


class McpTransport(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"
    WS = "ws"
    SDK = "sdk"


@dataclass(slots=True)
class ScopedMcpServerConfig:
    id: str
    scope: McpScope
    type: McpTransport = McpTransport.STDIO
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    source_path: str | None = None
    plugin_source: str | None = None


@dataclass(slots=True)
class McpConfigLoadResult:
    servers: list[ScopedMcpServerConfig]
    suppressed: list[dict[str, str]]
    blocked: list[str]
    warnings: list[str]


def load_scoped_mcp_servers(
    cwd: Path | None = None,
    *,
    explicit_config_path: str | None = None,
    dynamic_servers: list[dict[str, Any]] | None = None,
) -> McpConfigLoadResult:
    cwd = (cwd or Path.cwd()).resolve()
    warnings: list[str] = []
    blocked: list[str] = []
    scoped: list[ScopedMcpServerConfig] = []

    for path, scope in _config_sources(cwd, explicit_config_path):
        scoped.extend(_read_mcp_config_file(path, scope, warnings))
    for item in dynamic_servers or []:
        server = _normalize_server(item.get("id") or item.get("name"), item, McpScope.DYNAMIC, None, warnings)
        if server:
            scoped.append(server)

    allowed = []
    for server in scoped:
        if not _allowed_by_policy(server):
            blocked.append(server.id)
            continue
        allowed.append(server)

    deduped, suppressed = _dedupe_servers(allowed)
    return McpConfigLoadResult(servers=deduped, suppressed=suppressed, blocked=blocked, warnings=warnings)


def server_signature(server: ScopedMcpServerConfig) -> str | None:
    if server.type == McpTransport.STDIO and server.command:
        return "stdio:" + json.dumps([server.command, *server.args], ensure_ascii=False)
    if server.url:
        return "url:" + _unwrap_proxy_url(server.url)
    if server.type == McpTransport.SDK:
        return None
    return None


def _config_sources(cwd: Path, explicit_config_path: str | None) -> list[tuple[Path, McpScope]]:
    sources: list[tuple[Path, McpScope]] = []
    managed = os.getenv("K_AGENT_MANAGED_MCP_CONFIG")
    if managed:
        sources.append((Path(managed).expanduser(), McpScope.MANAGED))
    user = os.getenv("K_AGENT_USER_MCP_CONFIG") or str(Path.home() / ".k_agent" / "mcp.json")
    sources.append((Path(user).expanduser(), McpScope.USER))
    sources.append((cwd / ".mcp.json", McpScope.PROJECT))
    if explicit_config_path:
        sources.append((Path(explicit_config_path).expanduser(), McpScope.LOCAL))
    return sources


def _read_mcp_config_file(path: Path, scope: McpScope, warnings: list[str]) -> list[ScopedMcpServerConfig]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"invalid MCP config {path}: {exc}")
        return []
    raw_servers = payload.get("mcpServers")
    if raw_servers is None:
        raw_servers = {item.get("id"): item for item in payload.get("servers", []) if item.get("id")}
    if not isinstance(raw_servers, dict):
        warnings.append(f"invalid MCP servers block in {path}")
        return []
    servers = []
    for name, item in raw_servers.items():
        server = _normalize_server(str(name), item, scope, str(path), warnings)
        if server:
            servers.append(server)
    return servers


def _normalize_server(
    name: str | None,
    item: Any,
    scope: McpScope,
    source_path: str | None,
    warnings: list[str],
) -> ScopedMcpServerConfig | None:
    if not name or not isinstance(item, dict):
        warnings.append(f"invalid MCP server entry: {name}")
        return None
    transport = McpTransport(item.get("type", "stdio"))
    command = item.get("command")
    url = item.get("url")
    if transport == McpTransport.STDIO and not command:
        warnings.append(f"MCP server {name} missing command")
        return None
    if transport in {McpTransport.SSE, McpTransport.HTTP, McpTransport.WS} and not url:
        warnings.append(f"MCP server {name} missing url")
        return None
    return ScopedMcpServerConfig(
        id=_normalize_name(name),
        scope=scope,
        type=transport,
        command=command,
        args=[str(arg) for arg in item.get("args", [])],
        env={str(key): str(value) for key, value in item.get("env", {}).items()},
        url=url,
        headers={str(key): str(value) for key, value in item.get("headers", {}).items()},
        enabled=bool(item.get("enabled", True)),
        source_path=source_path,
        plugin_source=item.get("pluginSource"),
    )


def _dedupe_servers(servers: list[ScopedMcpServerConfig]) -> tuple[list[ScopedMcpServerConfig], list[dict[str, str]]]:
    by_name: dict[str, ScopedMcpServerConfig] = {}
    by_signature: dict[str, str] = {}
    suppressed: list[dict[str, str]] = []
    for server in servers:
        if server.id in by_name:
            suppressed.append({"name": server.id, "duplicateOf": server.id, "reason": "name"})
            continue
        signature = server_signature(server)
        if signature and signature in by_signature:
            suppressed.append({"name": server.id, "duplicateOf": by_signature[signature], "reason": "signature"})
            continue
        by_name[server.id] = server
        if signature:
            by_signature[signature] = server.id
    return list(by_name.values()), suppressed


def _allowed_by_policy(server: ScopedMcpServerConfig) -> bool:
    if not server.enabled:
        return False
    denied = _policy_list("K_AGENT_DENIED_MCP_SERVERS")
    allowed = _policy_list("K_AGENT_ALLOWED_MCP_SERVERS")
    if _matches_policy(server, denied):
        return False
    return not allowed or _matches_policy(server, allowed)


def _matches_policy(server: ScopedMcpServerConfig, entries: list[str]) -> bool:
    signature = server_signature(server) or ""
    command = " ".join([server.command or "", *server.args]).strip()
    values = [server.id, signature, command, server.url or ""]
    return any(any(_glob_match(value, entry) for value in values) for entry in entries)


def _policy_list(env_name: str) -> list[str]:
    raw = os.getenv(env_name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _glob_match(value: str, pattern: str) -> bool:
    regex = "^" + re.escape(pattern).replace("\\*", ".*") + "$"
    return re.match(regex, value) is not None


def _normalize_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip())
    return normalized.strip("_") or "mcp"


def _unwrap_proxy_url(url: str) -> str:
    for marker in ("/v2/session_ingress/shttp/mcp/", "/v2/ccr-sessions/"):
        if marker in url and "mcp_url=" in url:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(url)
            return parse_qs(parsed.query).get("mcp_url", [url])[0]
    return url
