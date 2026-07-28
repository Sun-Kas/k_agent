from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.websocket import websocket_client

from backend.config import get_or_init_settings
from backend.mcp_tool.config import McpConfigLoadResult, McpScope, McpTransport, ScopedMcpServerConfig, load_scoped_mcp_servers


@dataclass(slots=True)
class McpServerConfig:
    id: str
    scope: str
    type: str
    command: str
    args: list[str]
    env: dict[str, str]
    url: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool = True
    source_path: str | None = None


@dataclass(slots=True)
class McpToolDescriptor:
    server_id: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None


@dataclass(slots=True)
class McpServerStatus:
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
        self.config = config
        self._runner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Future[None] | None = None
        self.session: ClientSession | None = None
        self.instructions: str | None = None
        self.server_info: Any = None

    async def connect(self) -> None:
        if self.session is not None:
            return
        if self._runner is None:
            loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            self._ready = loop.create_future()
            self._runner = asyncio.create_task(self._run())
        if self._ready is not None:
            await asyncio.wait_for(asyncio.shield(self._ready), timeout=0.75)

    async def _run(self) -> None:
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
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env or None,
            )
            return stdio_client(server_params)
        if self.config.type == McpTransport.SSE.value:
            return sse_client(self.config.url or "", headers=self.config.headers)
        if self.config.type == McpTransport.HTTP.value:
            return streamablehttp_client(self.config.url or "", headers=self.config.headers)
        if self.config.type == McpTransport.WS.value:
            return websocket_client(self.config.url or "")
        raise RuntimeError(f"unsupported MCP transport: {self.config.type}")

    async def list_tools(self) -> list[McpToolDescriptor]:
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
        if self.session is None:
            raise RuntimeError("MCP session is not ready")
        result = await self.session.call_tool(tool_name, arguments)
        serialized = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item)
            for item in result.content
        ]
        return json.dumps(serialized, ensure_ascii=False)


McpStdioSession = McpSession


class McpClientManager:
    def __init__(self, servers: list[McpServerConfig], load_result: McpConfigLoadResult | None = None):
        self.servers = servers
        self.load_result = load_result
        self.sessions: dict[str, McpSession] = {}
        self.failed: dict[str, str] = {}
        self.disabled: set[str] = {server.id for server in servers if not server.enabled}

    async def connect_all(self) -> None:
        for server in self.servers:
            if not server.enabled:
                continue
            session = self.sessions.get(server.id)
            if session is None:
                session = McpSession(server)
                self.sessions[server.id] = session
            try:
                async with asyncio.timeout(3):
                    await session.connect()
            except Exception as exc:
                self.failed[server.id] = str(exc)
                continue

    async def list_tools(self) -> list[McpToolDescriptor]:
        tools: list[McpToolDescriptor] = []
        for session in self.sessions.values():
            try:
                tools.extend(await session.list_tools())
            except Exception:
                continue
        return tools

    async def list_resources(self) -> dict[str, list[dict[str, Any]]]:
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
        session = self.sessions.get(server_id)
        if session is None or session.session is None:
            raise RuntimeError(f'MCP server "{server_id}" is not connected.')
        result = await session.session.read_resource(uri)
        return json.dumps(
            result.model_dump(mode="json") if hasattr(result, "model_dump") else str(result),
            ensure_ascii=False,
        )

    async def statuses(self) -> list[McpServerStatus]:
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
        instructions = {}
        for server_id, session in self.sessions.items():
            value = getattr(session, "instructions", None)
            if value:
                instructions[server_id] = value
        return instructions

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
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
        for session in list(self.sessions.values()):
            try:
                await session.close()
            except Exception:
                continue
        self.sessions.clear()


async def load_mcp_servers() -> list[McpServerConfig]:
    settings = await get_or_init_settings()
    result = load_scoped_mcp_servers(explicit_config_path=settings.mcp_config_path)
    return [_to_legacy_config(server) for server in result.servers]


async def load_mcp_manager() -> McpClientManager:
    settings = await get_or_init_settings()
    result = load_scoped_mcp_servers(explicit_config_path=settings.mcp_config_path)
    return McpClientManager([_to_legacy_config(server) for server in result.servers], result)


def _to_legacy_config(server: ScopedMcpServerConfig) -> McpServerConfig:
    return McpServerConfig(
        id=server.id,
        scope=server.scope.value if isinstance(server.scope, McpScope) else str(server.scope),
        type=server.type.value if isinstance(server.type, McpTransport) else str(server.type),
        command=server.command or "",
        args=server.args,
        env=server.env,
        url=server.url,
        headers=server.headers,
        enabled=server.enabled,
        source_path=server.source_path,
    )
