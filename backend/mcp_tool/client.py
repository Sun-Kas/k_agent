from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.config import get_or_init_settings


@dataclass(slots=True)
class McpServerConfig:
    id: str
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass(slots=True)
class McpToolDescriptor:
    server_id: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None


class McpStdioSession:
    def __init__(self, config: McpServerConfig):
        self.config = config
        self._runner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Future[None] | None = None
        self.session: ClientSession | None = None

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
        server_params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.config.env or None,
        )
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await session.initialize()
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


class McpClientManager:
    def __init__(self, servers: list[McpServerConfig]):
        self.servers = servers
        self.sessions: dict[str, McpStdioSession] = {}

    async def connect_all(self) -> None:
        for server in self.servers:
            session = self.sessions.get(server.id)
            if session is None:
                session = McpStdioSession(server)
                self.sessions[server.id] = session
            try:
                async with asyncio.timeout(3):
                    await session.connect()
            except Exception:
                continue

    async def list_tools(self) -> list[McpToolDescriptor]:
        tools: list[McpToolDescriptor] = []
        for session in self.sessions.values():
            try:
                tools.extend(await session.list_tools())
            except Exception:
                continue
        return tools

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        session = self.sessions.get(server_id)
        if session is None:
            raise RuntimeError(f'MCP server "{server_id}" is not connected.')
        return await session.call_tool(tool_name, arguments)

    async def close_all(self) -> None:
        for session in list(self.sessions.values()):
            try:
                await session.close()
            except Exception:
                continue
        self.sessions.clear()


async def load_mcp_servers() -> list[McpServerConfig]:
    settings = await get_or_init_settings()
    config_path = Path(settings.mcp_config_path)
    if not config_path.exists():
        return []

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    servers = []
    for item in payload.get("servers", []):
        if not item.get("enabled", True):
            continue
        servers.append(
            McpServerConfig(
                id=item["id"],
                command=item["command"],
                args=item.get("args", []),
                env=item.get("env", {}),
            )
        )
    return servers
