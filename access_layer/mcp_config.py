"""Load, normalize, merge, and policy-filter MCP server configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class McpScope(str, Enum):
    """MCP 配置来源范围。加载顺序（先出现的同名留下）：MANAGED → USER → PROJECT → LOCAL → DYNAMIC。

    下列路径均指连接配置（Claude 风格 mcpServers），不是 catalog/mcp.json。
    """

    # `$K_AGENT_HOME/config/mcp.json`（可用 MCP_CONFIG_PATH / explicit_config_path 覆盖）。
    # 配置中心与连接器市场的写入处。
    LOCAL = "local"
    # `$K_AGENT_USER_MCP_CONFIG`，未设则为 `$K_AGENT_HOME/config/user-mcp.json`。
    USER = "user"
    # `{cwd}/.mcp.json`；cwd 未传入时是 Access Layer 进程当前目录。
    PROJECT = "project"
    # 无文件；请求内注入，当前无调用方。
    DYNAMIC = "dynamic"
    # `$K_AGENT_MANAGED_MCP_CONFIG` 指向的文件；未设环境变量则无此档。
    MANAGED = "managed"
    # 无路径；枚举预留，加载尚未接入。
    PLUGIN = "plugin"


class McpTransport(str, Enum):
    """枚举 MCP 连接传输类型。"""
    STDIO = "stdio"
    HTTP = "http"


@dataclass(slots=True)
class ScopedMcpServerConfig:
    """描述带来源范围的 MCP server 配置。"""
    id: str
    scope: McpScope
    type: McpTransport = McpTransport.STDIO
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: list[str] = field(default_factory=list)
    cwd: str | None = None
    url: str | None = None
    bearer_token_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env_headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    source_path: str | None = None
    plugin_source: str | None = None


@dataclass(slots=True)
class McpConfigLoadResult:
    """承载 MCP 配置加载结果、警告和过滤记录。"""
    servers: list[ScopedMcpServerConfig]
    suppressed: list[dict[str, str]]
    blocked: list[str]
    warnings: list[str]


def load_scoped_mcp_servers(
    cwd: Path | None = None,
    *,
    explicit_config_path: str | None = None,
    # 预留：不落盘、请求内注入的 MCP。当前无调用方传入，工作台 MCP 只走配置文件。
    dynamic_servers: list[dict[str, Any]] | None = None,
) -> McpConfigLoadResult:
    """按优先级加载、策略过滤并去重 MCP server，供 run 时装配连接。"""
    # 解析项目级 `.mcp.json` 用的工作目录；未传入则用本进程 cwd。
    cwd = (cwd or Path.cwd()).resolve()
    # 配置损坏、缺字段等非致命问题，最后随结果返回。
    warnings: list[str] = []
    # 被 enabled / allow-deny 策略丢掉的 server id。
    blocked: list[str] = []
    # 各层解析出的原始 server（含 scope），尚未过滤、去重。
    scoped: list[ScopedMcpServerConfig] = []

    # 1. 按 MANAGED → USER → PROJECT → LOCAL 读入各层配置（缺文件则跳过）。
    for path, scope in _config_sources(cwd, explicit_config_path):
        scoped.extend(_read_mcp_config_file(path, scope, warnings))
    # 2. 预留的动态注入（scope=DYNAMIC），优先级最低；现无产品路径传参。
    for item in dynamic_servers or []:
        server = _normalize_server(item.get("id") or item.get("name"), item, McpScope.DYNAMIC, None, warnings)
        if server:
            scoped.append(server)

    # 3. enabled / allow-deny 名单过滤；被拒的 id 记入 blocked，不进入结果。
    allowed = []
    for server in scoped:
        if not _allowed_by_policy(server):
            blocked.append(server.id)
            continue
        allowed.append(server)

    # 4. 同 id 或同连接签名只留先出现的；被挤掉的记入 suppressed。
    deduped, suppressed = _dedupe_servers(allowed)
    return McpConfigLoadResult(servers=deduped, suppressed=suppressed, blocked=blocked, warnings=warnings)


def server_signature(server: ScopedMcpServerConfig) -> str | None:
    """生成 MCP server 去重签名。"""
    if server.type == McpTransport.STDIO and server.command:
        return "stdio:" + json.dumps([server.command, *server.args], ensure_ascii=False)
    if server.url:
        return "url:" + _unwrap_proxy_url(server.url)
    return None


def _config_sources(cwd: Path, explicit_config_path: str | None) -> list[tuple[Path, McpScope]]:
    """按覆盖优先级列出候选 MCP 配置文件（路径 + scope），不读内容。

    调用方按此顺序加载；同名 server 先出现的留下。文件不存在由读取侧跳过。
    """
    sources: list[tuple[Path, McpScope]] = []
    managed = os.getenv("K_AGENT_MANAGED_MCP_CONFIG")
    if managed:
        # 运维注入的托管配置，优先级最高。
        sources.append((Path(managed).expanduser(), McpScope.MANAGED))
    from access_layer.home import user_mcp_config_path

    # 用户级覆盖；未设环境变量时默认 `$K_AGENT_HOME/config/user-mcp.json`。
    user = os.getenv("K_AGENT_USER_MCP_CONFIG") or str(user_mcp_config_path())
    sources.append((Path(user).expanduser(), McpScope.USER))
    # 当前工作区根的项目配置。
    sources.append((cwd / ".mcp.json", McpScope.PROJECT))
    if explicit_config_path:
        # Access Layer 传入的本机托管 mcp.json，同名时被上面几档盖掉。
        sources.append((Path(explicit_config_path).expanduser(), McpScope.LOCAL))
    return sources


def _read_mcp_config_file(path: Path, scope: McpScope, warnings: list[str]) -> list[ScopedMcpServerConfig]:
    """读取单个 MCP 连接配置。只认 Claude 风格 `{ "mcpServers": { id: {...} } }`。

    旧版 `{ "servers": [{ "id": ... }] }` 不再读取。catalog/mcp.json 仍用 `servers` 数组，那是选择器摘要，不是本函数。
    """
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"invalid MCP config {path}: {exc}")
        return []
    # id 是 mcpServers 对象的 key：{ "mcpServers": { "fs": { "command": "npx" } } }
    raw_servers = payload.get("mcpServers")
    if raw_servers is None:
        return []
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
    """把原始 MCP server 条目规范化为内部结构。"""
    if not name or not isinstance(item, dict):
        warnings.append(f"invalid MCP server entry: {name}")
        return None
    raw_transport = item.get("type", "stdio")
    if raw_transport == "sse":
        raw_transport = "http"
    try:
        transport = McpTransport(raw_transport)
    except ValueError:
        warnings.append(f"MCP server {name} unsupported transport: {raw_transport}")
        return None
    command = item.get("command")
    url = item.get("url")
    if transport == McpTransport.STDIO and not command:
        warnings.append(f"MCP server {name} missing command")
        return None
    if transport == McpTransport.HTTP and not url:
        warnings.append(f"MCP server {name} missing url")
        return None
    return ScopedMcpServerConfig(
        id=_normalize_name(name),
        scope=scope,
        type=transport,
        command=command,
        args=[str(arg) for arg in item.get("args", [])],
        env={str(key): str(value) for key, value in item.get("env", {}).items()},
        env_passthrough=[str(value) for value in item.get("envPassthrough", [])],
        cwd=str(item.get("cwd")) if item.get("cwd") else None,
        url=url,
        bearer_token_env=str(item.get("bearerTokenEnv")) if item.get("bearerTokenEnv") else None,
        headers={str(key): str(value) for key, value in item.get("headers", {}).items()},
        env_headers={str(key): str(value) for key, value in item.get("envHeaders", {}).items()},
        enabled=bool(item.get("enabled", True)),
        source_path=source_path,
        plugin_source=item.get("pluginSource"),
    )


def _dedupe_servers(servers: list[ScopedMcpServerConfig]) -> tuple[list[ScopedMcpServerConfig], list[dict[str, str]]]:
    """按签名去重 MCP server 并记录被抑制项。"""
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
    """进程级 allow/deny 名单，比配置文件里的 enabled 更硬。

    环境变量为逗号分隔 glob，可匹配 id、连接签名、command 行或 url。
    deny 优先；allow 为空表示不额外限制（仍要过 enabled）。
    """
    if not server.enabled:
        return False
    denied = _policy_list("K_AGENT_DENIED_MCP_SERVERS")
    allowed = _policy_list("K_AGENT_ALLOWED_MCP_SERVERS")
    # 命中拒绝名单直接丢掉，即使也在 allow 里。
    if _matches_policy(server, denied):
        return False
    # 未配置 allow → 放行；配了则必须命中其中一条。
    return not allowed or _matches_policy(server, allowed)


def _matches_policy(server: ScopedMcpServerConfig, entries: list[str]) -> bool:
    """条目与下列任一字符串做整段 glob 匹配（仅 * 通配，区分大小写）即算命中：
    id、stdio 签名 ``stdio:["cmd","arg"]``、``command arg...``、url。
    """
    signature = server_signature(server) or ""
    command = " ".join([server.command or "", *server.args]).strip()
    values = [server.id, signature, command, server.url or ""]
    return any(any(_glob_match(value, entry) for value in values) for entry in entries)


def _policy_list(env_name: str) -> list[str]:
    """``K_AGENT_DENIED_MCP_SERVERS`` / ``K_AGENT_ALLOWED_MCP_SERVERS``：逗号分隔，无 JSON。
    例：``filesystem,npx -y @evil/*,https://mcp.company/*``
    """
    raw = os.getenv(env_name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _glob_match(value: str, pattern: str) -> bool:
    """执行大小写不敏感的 glob 匹配。"""
    regex = "^" + re.escape(pattern).replace("\\*", ".*") + "$"
    return re.match(regex, value) is not None


def _normalize_name(name: str) -> str:
    """把名称规范化为稳定的 MCP server ID。"""
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip())
    return normalized.strip("_") or "mcp"


def _unwrap_proxy_url(url: str) -> str:
    """从代理 URL 中还原真实目标 URL。"""
    for marker in ("/v2/session_ingress/shttp/mcp/", "/v2/ccr-sessions/"):
        if marker in url and "mcp_url=" in url:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(url)
            return parse_qs(parsed.query).get("mcp_url", [url])[0]
    return url
