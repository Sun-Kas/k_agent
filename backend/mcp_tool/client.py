"""Lifecycle management and capability access for configured MCP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from backend.config import get_or_init_settings
from backend.logging_config import log_event
from backend.mcp_tool.config import McpConfigLoadResult, McpScope, McpTransport, ScopedMcpServerConfig, load_scoped_mcp_servers


@dataclass(slots=True)
class McpServerConfig:
    """Normalized connection settings for one MCP server."""

    id: str
    scope: str
    type: str
    command: str
    args: list[str]
    env: dict[str, str]
    env_passthrough: list[str] | None = None
    cwd: str | None = None
    url: str | None = None
    bearer_token_env: str | None = None
    headers: dict[str, str] | None = None
    env_headers: dict[str, str] | None = None
    enabled: bool = True
    source_path: str | None = None


@dataclass(slots=True)
class McpToolDescriptor:
    """Tool metadata annotated with the owning server identifier."""

    server_id: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None


@dataclass(slots=True)
class McpServerStatus:
    """Serializable connection and capability summary for configuration APIs."""

    id: str
    scope: str
    type: str
    status: str
    tool_count: int = 0
    resource_count: int = 0
    instructions: str | None = None
    error: str | None = None


class McpSession:
    """Owns one long-lived MCP client connection.

    Claude Code treats each configured server as a persistent connection with a
    status. We use the same shape here: connect once, keep the SDK session alive
    in a background task, and let tools/resources/prompts share that connection.
    """

    def __init__(self, config: McpServerConfig):
        """初始化对象依赖和内部状态。"""
        self.config = config
        self._runner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Future[None] | None = None
        self.session: ClientSession | None = None
        self.instructions: str | None = None
        self.server_info: Any = None

    async def connect(self) -> None:
        """连接并初始化单个 MCP server 会话。"""
        if self.session is not None:
            return
        if self._runner is None:
            loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            self._ready = loop.create_future()
            self._runner = asyncio.create_task(self._run())
        if self._ready is not None:
            # uvx/npx may download the MCP package on first use. The manager
            # owns the configurable timeout, so this lifecycle layer must not
            # reject an otherwise healthy cold start after a fraction of a second.
            await asyncio.shield(self._ready)

    async def _run(self) -> None:
        """启动 MCP stdio 进程并建立底层 ClientSession。"""
        try:
            async with self._open_transport() as streams:
                read, write = streams[:2]
                async with ClientSession(read, write) as session:
                    self.session = session
                    init = await session.initialize()
                    self.instructions = getattr(init, "instructions", None)
                    self.server_info = getattr(init, "serverInfo", None)
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                    if self._stop is not None:
                        await self._stop.wait()
        except asyncio.CancelledError:
            if self._ready is not None and not self._ready.done():
                self._ready.cancel()
            raise
        except BaseException as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self.session = None

    def _open_transport(self):
        """Return the SDK transport context manager for the configured server.

        Stdio is the common local-server path. HTTP/SSE/WS are remote transports
        used by connector-style MCP servers; all feed the same ClientSession API.
        """
        if self.config.type == McpTransport.STDIO.value:
            env = dict(self.config.env or {})
            for name in self.config.env_passthrough or []:
                if name in os.environ:
                    env[name] = os.environ[name]
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=env or None,
                cwd=self.config.cwd,
            )
            return stdio_client(server_params)
        if self.config.type == McpTransport.HTTP.value:
            headers = dict(self.config.headers or {})
            for key, env_name in (self.config.env_headers or {}).items():
                if env_name in os.environ:
                    headers[key] = os.environ[env_name]
            if self.config.bearer_token_env:
                token = os.getenv(self.config.bearer_token_env)
                if not token:
                    raise RuntimeError(
                        "Configured Bearer token environment variable is not set"
                    )
                headers["Authorization"] = f"Bearer {token}"
            return streamablehttp_client(self.config.url or "", headers=headers or None)
        raise RuntimeError(f"unsupported MCP transport: {self.config.type}")

    async def list_tools(self) -> list[McpToolDescriptor]:
        """列出当前对象可用的工具定义。"""
        if self.session is None:
            raise RuntimeError("MCP session is not ready")

        result = await self.session.list_tools()
        tools = result.tools
        return [
            McpToolDescriptor(
                server_id=self.config.id,
                name=tool.name,
                description=tool.description,
                input_schema=getattr(tool, "inputSchema", None),
            )
            for tool in tools
        ]

    async def close(self) -> None:
        """关闭当前会话和底层传输资源。"""
        if self._stop is not None:
            self._stop.set()
        if self._runner is not None:
            try:
                await asyncio.wait_for(self._runner, timeout=3)
            except (TimeoutError, asyncio.CancelledError):
                self._runner.cancel()
        self._runner = None
        self._stop = None
        self._ready = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """调用指定工具并返回文本化结果。"""
        if self.session is None:
            raise RuntimeError("MCP session is not ready")
        result = await self.session.call_tool(tool_name, arguments)
        serialized = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item)
            for item in result.content
        ]
        if getattr(result, "isError", False):
            # MCP can report a tool-level failure without raising. Preserve its
            # content in the same recoverable contract used by local tools.
            reason = "\n".join(
                str(item.get("text"))
                for item in serialized
                if isinstance(item, dict) and item.get("text")
            ) or "MCP tool reported an execution error."
            return json.dumps(
                {
                    "ok": False,
                    "tool": tool_name,
                    "error": reason,
                    "errorType": "McpToolError",
                    "content": serialized,
                },
                ensure_ascii=False,
            )
        return json.dumps(serialized, ensure_ascii=False)


McpStdioSession = McpSession


class McpClientManager:
    """Coordinate independent MCP sessions while isolating per-server failures."""

    def __init__(
        self,
        servers: list[McpServerConfig],
        load_result: McpConfigLoadResult | None = None,
        *,
        log_context: dict[str, Any] | None = None,
        connect_timeout_seconds: float = 60.0,
    ):
        """初始化对象依赖和内部状态。"""
        self.servers = servers
        self.load_result = load_result
        self.log_context = dict(log_context or {})
        self.connect_timeout_seconds = connect_timeout_seconds
        self.sessions: dict[str, McpSession] = {}
        self.failed: dict[str, str] = {}
        self.disabled: set[str] = {server.id for server in servers if not server.enabled}

    async def connect_all(self) -> None:
        """Connect enabled servers without letting one failure block the others."""

        log_event(
            "mcp.load.started",
            **self.log_context,
            serverCount=len(self.servers),
            enabledServerCount=sum(server.enabled for server in self.servers),
            disabledServerCount=sum(not server.enabled for server in self.servers),
            suppressedServerCount=len(self.load_result.suppressed) if self.load_result else 0,
            blockedServerCount=len(self.load_result.blocked) if self.load_result else 0,
            warningCount=len(self.load_result.warnings) if self.load_result else 0,
        )
        for server in self.servers:
            if not server.enabled:
                log_event(
                    "mcp.server.disabled",
                    **self.log_context,
                    serverId=server.id,
                    transport=server.type,
                )
                continue
            session = self.sessions.get(server.id)
            if session is None:
                session = McpSession(server)
                self.sessions[server.id] = session
            started_at = time.perf_counter()
            log_event(
                "mcp.server.connect.started",
                **self.log_context,
                serverId=server.id,
                transport=server.type,
                scope=server.scope,
            )
            try:
                async with asyncio.timeout(self.connect_timeout_seconds):
                    await session.connect()
            except Exception as exc:
                # Do not leave a timed-out uvx/npx child process downloading in
                # the background while this manager reports the server failed.
                await session.close()
                self.failed[server.id] = str(exc)
                log_event(
                    "mcp.server.connect.failed",
                    level=logging.ERROR,
                    **self.log_context,
                    serverId=server.id,
                    transport=server.type,
                    elapsedMs=round(
                        (time.perf_counter() - started_at) * 1000,
                        3,
                    ),
                    errorType=type(exc).__name__,
                )
                continue
            log_event(
                "mcp.server.connect.completed",
                **self.log_context,
                serverId=server.id,
                transport=server.type,
                elapsedMs=round(
                    (time.perf_counter() - started_at) * 1000,
                    3,
                ),
            )
        log_event(
            "mcp.load.completed",
            **self.log_context,
            connectedServerCount=sum(
                session.session is not None for session in self.sessions.values()
            ),
            failedServerCount=len(self.failed),
        )

    async def list_tools(self) -> list[McpToolDescriptor]:
        """列出当前对象可用的工具定义。"""
        tools: list[McpToolDescriptor] = []
        for session in self.sessions.values():
            try:
                tools.extend(await session.list_tools())
            except Exception:
                continue
        log_event(
            "mcp.tools.loaded",
            **self.log_context,
            connectedServerCount=sum(
                session.session is not None for session in self.sessions.values()
            ),
            toolCount=len(tools),
        )
        return tools

    async def list_resources(self) -> dict[str, list[dict[str, Any]]]:
        """列出已连接 MCP server 暴露的资源。"""
        resources: dict[str, list[dict[str, Any]]] = {}
        for server_id, session in self.sessions.items():
            if session.session is None:
                continue
            try:
                result = await session.session.list_resources()
            except Exception:
                continue
            resources[server_id] = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else {"uri": str(item)}
                for item in getattr(result, "resources", [])
            ]
        return resources

    async def list_prompts(self) -> dict[str, list[dict[str, Any]]]:
        """列出已连接 MCP server 暴露的 prompts。"""
        prompts: dict[str, list[dict[str, Any]]] = {}
        for server_id, session in self.sessions.items():
            if session.session is None:
                continue
            try:
                result = await session.session.list_prompts()
            except Exception:
                continue
            prompts[server_id] = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else {"name": str(item)}
                for item in getattr(result, "prompts", [])
            ]
        return prompts

    async def read_resource(self, server_id: str, uri: str) -> str:
        """读取指定 MCP server 的 resource 内容。"""
        session = self.sessions.get(server_id)
        if session is None or session.session is None:
            raise RuntimeError(f'MCP server "{server_id}" is not connected.')
        result = await session.session.read_resource(uri)
        return json.dumps(
            result.model_dump(mode="json") if hasattr(result, "model_dump") else str(result),
            ensure_ascii=False,
        )

    async def statuses(self) -> list[McpServerStatus]:
        """汇总所有 MCP server 的连接状态。"""
        tools = await self.list_tools()
        resources = await self.list_resources()
        tool_counts: dict[str, int] = {}
        for tool in tools:
            tool_counts[tool.server_id] = tool_counts.get(tool.server_id, 0) + 1
        statuses = []
        for server in self.servers:
            session = self.sessions.get(server.id)
            status = "disabled" if not server.enabled else "connected" if session and session.session else "failed" if server.id in self.failed else "pending"
            statuses.append(
                McpServerStatus(
                    id=server.id,
                    scope=server.scope,
                    type=server.type,
                    status=status,
                    tool_count=tool_counts.get(server.id, 0),
                    resource_count=len(resources.get(server.id, [])),
                    instructions=getattr(session, "instructions", None) if session else None,
                    error=self.failed.get(server.id),
                )
            )
        return statuses

    def connected_instructions(self) -> dict[str, str]:
        """收集已连接 MCP server 的动态指令文本。"""
        instructions = {}
        for server_id, session in self.sessions.items():
            value = getattr(session, "instructions", None)
            if value:
                instructions[server_id] = value
        return instructions

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """调用指定工具并返回文本化结果。"""
        session = self.sessions.get(server_id)
        if session is None:
            raise RuntimeError(f'MCP server "{server_id}" is not connected.')
        return await session.call_tool(tool_name, arguments)

    async def call_prompt(self, server_id: str, prompt_name: str, arguments: dict[str, Any]) -> str:
        """Invoke an MCP prompt and serialize the returned prompt messages."""
        session = self.sessions.get(server_id)
        if session is None or session.session is None:
            raise RuntimeError(f'MCP server "{server_id}" is not connected.')
        result = await session.session.get_prompt(prompt_name, arguments)
        return json.dumps(
            result.model_dump(mode="json") if hasattr(result, "model_dump") else str(result),
            ensure_ascii=False,
        )

    async def close_all(self) -> None:
        """关闭 manager 管理的全部 MCP 会话。"""
        for session in list(self.sessions.values()):
            try:
                await session.close()
            except Exception:
                continue
        self.sessions.clear()


async def load_mcp_servers() -> list[McpServerConfig]:
    """读取 scoped MCP 配置并转换为客户端连接配置。"""
    settings = await get_or_init_settings()
    result = load_scoped_mcp_servers(explicit_config_path=settings.mcp_config_path)
    return [_to_legacy_config(server) for server in result.servers]


async def load_mcp_manager() -> McpClientManager:
    """创建加载好配置的 MCP 客户端管理器。"""
    settings = await get_or_init_settings()
    result = load_scoped_mcp_servers(explicit_config_path=settings.mcp_config_path)
    return McpClientManager(
        [_to_legacy_config(server) for server in result.servers],
        result,
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
    )


def mcp_manager_from_runtime(
    servers: list[dict[str, Any]],
    *,
    log_context: dict[str, Any] | None = None,
    connect_timeout_seconds: float = 60.0,
) -> McpClientManager:
    """Build a manager only from access-layer supplied runtime definitions."""
    configs = [
        McpServerConfig(
            id=str(server["id"]),
            scope=str(server.get("scope") or "local"),
            type=str(server.get("type") or "stdio"),
            command=str(server.get("command") or ""),
            args=[str(value) for value in server.get("args", [])],
            env={
                str(key): str(value)
                for key, value in server.get("env", {}).items()
            },
            env_passthrough=[
                str(value) for value in server.get("envPassthrough", [])
            ],
            cwd=str(server["cwd"]) if server.get("cwd") else None,
            url=str(server["url"]) if server.get("url") else None,
            bearer_token_env=(
                str(server["bearerTokenEnv"])
                if server.get("bearerTokenEnv")
                else None
            ),
            headers={
                str(key): str(value)
                for key, value in server.get("headers", {}).items()
            },
            env_headers={
                str(key): str(value)
                for key, value in server.get("envHeaders", {}).items()
            },
            enabled=bool(server.get("enabled", True)),
            source_path=(
                str(server["sourcePath"]) if server.get("sourcePath") else None
            ),
        )
        for server in servers
    ]
    return McpClientManager(
        configs,
        log_context=log_context,
        connect_timeout_seconds=connect_timeout_seconds,
    )


def _to_legacy_config(server: ScopedMcpServerConfig) -> McpServerConfig:
    """把 scoped MCP 配置转换成旧版客户端配置结构。"""
    return McpServerConfig(
        id=server.id,
        scope=server.scope.value if isinstance(server.scope, McpScope) else str(server.scope),
        type=server.type.value if isinstance(server.type, McpTransport) else str(server.type),
        command=server.command or "",
        args=server.args,
        env=server.env,
        env_passthrough=server.env_passthrough,
        cwd=server.cwd,
        url=server.url,
        bearer_token_env=server.bearer_token_env,
        headers=server.headers,
        env_headers=server.env_headers,
        enabled=server.enabled,
        source_path=server.source_path,
    )
