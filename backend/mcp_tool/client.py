"""已配置 MCP server 的连接生命周期与能力访问。

pipeline：Access Layer 下发本轮 server 列表 → `McpClientManager`（可借
`McpSessionPool`）连接 → Agent/工具调用 list/call；单 server 失败不拖垮其它。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from backend.config import get_or_init_settings
from backend.logging_config import log_event
from backend.mcp_tool.config import McpConfigLoadResult, McpScope, McpTransport, ScopedMcpServerConfig, load_scoped_mcp_servers
from backend.mcp_tool.stderr import McpStderrBridge

if TYPE_CHECKING:
    from backend.mcp_tool.pool import McpSessionPool


@dataclass(slots=True)
class McpServerConfig:
    """单个 MCP server 的规范化连接设置。"""

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
    """带所属 server_id 注解的工具元数据。"""

    server_id: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None


@dataclass(slots=True)
class McpServerStatus:
    """可序列化的连接与能力摘要，供配置/健康 API 使用。"""

    id: str
    scope: str
    type: str
    status: str
    tool_count: int = 0
    resource_count: int = 0
    # Handshake usage hint copied from the live session, if the server sent one.
    instructions: str | None = None
    error: str | None = None


class McpSession:
    """持有一条长生命周期 MCP 客户端连接。

    与 Claude Code 类似：每个配置的 server 是持久连接 + 状态。后台任务持有
    SDK 传输上下文，tools/resources/prompts 共享同一会话。
    """

    def __init__(self, config: McpServerConfig):
        """登记配置；真正的传输在 `_run` 后台任务里进入/退出。"""
        self.config = config
        # SDK 的传输层是异步上下文管理器，必须在同一个任务里进入和退出。
        # 因此用一个常驻后台任务 _runner 持有它，再靠 _ready / _stop 两个
        # 信号和外部协调：_ready 表示握手完成可以用，_stop 表示请求退出。
        self._runner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Future[None] | None = None
        self.session: ClientSession | None = None
        # MCP InitializeResult.instructions — optional server-written usage hint.
        self.instructions: str | None = None
        self.server_info: Any = None
        # 桥接对象需与会话同寿命，否则 GC 后 MCP 会回落到进程 stderr。
        self._stderr_bridge: McpStderrBridge | None = None

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
                    # Spec field, not tools/list. Many servers leave this unset.
                    self.instructions = getattr(init, "instructions", None)
                    self.server_info = getattr(init, "serverInfo", None)
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                    # 停在这里不返回，让上面两层 async with 保持打开；
                    # 一旦返回，传输和会话就会被关掉。
                    if self._stop is not None:
                        await self._stop.wait()
        except asyncio.CancelledError:
            if self._ready is not None and not self._ready.done():
                self._ready.cancel()
            raise
        except BaseException as exc:
            # 捕到 BaseException 是刻意的：连接失败必须落到 _ready 上，
            # 否则 connect() 会永远等待一个再也不会完成的 future。
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
            if self._stderr_bridge is None:
                self._stderr_bridge = McpStderrBridge(self.config.id)
            return stdio_client(server_params, errlog=self._stderr_bridge)
        if self.config.type == McpTransport.HTTP.value:
            headers = dict(self.config.headers or {})
            for key, env_name in (self.config.env_headers or {}).items():
                if env_name in os.environ:
                    headers[key] = os.environ[env_name]
            # 令牌只允许通过环境变量名间接引用，配置文件里存的是变量名而非明文，
            # 避免凭据被写进仓库或通过配置接口读出。
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
        """向当前 MCP 会话请求 tools/list，并转成内部描述符。"""
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
            # 先给 3 秒让它走正常收尾（子进程优雅退出、HTTP 连接关闭）；
            # 超时则强制取消，不能让一个卡死的 server 拖住整个请求的结束。
            try:
                await asyncio.wait_for(self._runner, timeout=3)
            except (TimeoutError, asyncio.CancelledError):
                self._runner.cancel()
        self._runner = None
        self._stop = None
        self._ready = None
        if self._stderr_bridge is not None:
            self._stderr_bridge.close()
            self._stderr_bridge = None

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


_TRANSIENT_RESULT_PATTERNS: tuple[str, ...] = (
    "user cancelled",
    "connection reset",
    "connection closed",
    "stream ended unexpectedly",
)


def _is_transient_result(result_json: str) -> bool:
    """Detect MCP results that indicate a transient/cancelled call worth retrying."""
    try:
        payload = json.loads(result_json)
    except (TypeError, json.JSONDecodeError):
        return False
    if isinstance(payload, dict) and not payload.get("ok", True):
        error_text = str(payload.get("error", "")).lower()
        return any(pattern in error_text for pattern in _TRANSIENT_RESULT_PATTERNS)
    if isinstance(payload, list):
        text = " ".join(
            str(item.get("text", "")).lower()
            for item in payload
            if isinstance(item, dict)
        )
        return any(pattern in text for pattern in _TRANSIENT_RESULT_PATTERNS)
    return False


class McpClientManager:
    """协调多个独立 MCP 会话，隔离单 server 失败。"""

    def __init__(
        self,
        servers: list[McpServerConfig],
        load_result: McpConfigLoadResult | None = None,
        *,
        log_context: dict[str, Any] | None = None,
        connect_timeout_seconds: float = 60.0,
        call_timeout_seconds: float | None = None,
        max_call_retries: int = 0,
        retry_base_delay_seconds: float = 1.0,
        session_pool: "McpSessionPool | None" = None,
    ):
        """可注入进程级 `session_pool`：借共享连接，`close_all` 时归还而非杀进程。"""
        self.servers = servers
        self.load_result = load_result
        self.log_context = dict(log_context or {})
        self.connect_timeout_seconds = connect_timeout_seconds
        self.call_timeout_seconds = call_timeout_seconds
        self.max_call_retries = max_call_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        # 有池时本 manager 借用共享连接，close_all 归还而非每轮杀掉子进程。
        self._session_pool = session_pool
        self._leases: dict[str, str] = {}
        self.sessions: dict[str, McpSession] = {}
        self.failed: dict[str, str] = {}
        self.disabled: set[str] = {server.id for server in servers if not server.enabled}

    async def connect_all(self) -> None:
        """连接已启用 server；单个失败记入 `failed`，不阻塞其余。"""

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
            started_at = time.perf_counter()
            log_event(
                "mcp.server.connect.started",
                **self.log_context,
                serverId=server.id,
                transport=server.type,
                scope=server.scope,
            )
            session = self.sessions.get(server.id)
            try:
                if self._session_pool is not None:
                    fingerprint, session = await self._session_pool.acquire(
                        server,
                        connect_timeout_seconds=self.connect_timeout_seconds,
                    )
                    self._leases[server.id] = fingerprint
                else:
                    if session is None:
                        session = McpSession(server)
                    async with asyncio.timeout(self.connect_timeout_seconds):
                        await session.connect()
                self.sessions[server.id] = session
            except Exception as exc:
                # Do not leave a timed-out uvx/npx child process downloading in
                # the background while this manager reports the server failed.
                self.sessions.pop(server.id, None)
                if session is not None and self._session_pool is None:
                    await session.close()
                self.failed[server.id] = str(exc)
                # OSError/FileNotFoundError details are usually fd/path issues,
                # not provider payloads — keep a short hint for operators.
                detail = None
                if isinstance(exc, (OSError, FileNotFoundError, TimeoutError)):
                    detail = str(exc).replace("\n", " ").strip()[:160] or None
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
                    errorDetail=detail,
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
        """汇总所有已连接 MCP server 的工具描述符（单 server 失败则跳过）。"""
        tools: list[McpToolDescriptor] = []
        for session in self.sessions.values():
            # 单个 server 查询失败只影响它自己的工具，其余照常暴露给模型。
            # 这类失败在 connect_all 阶段已经记过日志，这里不再重复刷屏。
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
        async with self._call_timeout():
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
            # 判定顺序即优先级：手动禁用 > 已连上 > 连接失败 > 尚未尝试。
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
        """已连接 server 在 initialize 时返回的 `instructions`（有才收录）。"""
        instructions = {}
        for server_id, session in self.sessions.items():
            value = getattr(session, "instructions", None)
            if value:
                instructions[server_id] = value
        return instructions

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """调用指定工具并返回文本化结果，对瞬时失败自动重试。"""
        session = self.sessions.get(server_id)
        if session is None:
            raise RuntimeError(f'MCP server "{server_id}" is not connected.')

        last_error: Exception | None = None
        last_result: str | None = None

        for attempt in range(1 + self.max_call_retries):
            try:
                async with self._call_timeout():
                    result = await session.call_tool(tool_name, arguments)
            except asyncio.CancelledError:
                # Run-level cancellation must propagate immediately.
                raise
            except Exception as exc:
                last_error = exc
                # Only retry transport/connection failures; business-logic
                # errors from the server (RuntimeError, ValueError) propagate
                # immediately so the model sees them without pointless delay.
                retryable = isinstance(exc, (TimeoutError, OSError, ConnectionError, EOFError))
                if retryable and attempt < self.max_call_retries:
                    delay = self.retry_base_delay_seconds * (2 ** attempt)
                    log_event(
                        "mcp.call.retry",
                        **self.log_context,
                        serverId=server_id,
                        tool=tool_name,
                        attempt=attempt + 1,
                        maxRetries=self.max_call_retries,
                        errorType=type(exc).__name__,
                        delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            # Retry transient result-level failures (e.g. "user cancelled MCP tool call").
            if _is_transient_result(result) and attempt < self.max_call_retries:
                last_result = result
                delay = self.retry_base_delay_seconds * (2 ** attempt)
                log_event(
                    "mcp.call.retry",
                    **self.log_context,
                    serverId=server_id,
                    tool=tool_name,
                    attempt=attempt + 1,
                    maxRetries=self.max_call_retries,
                    reason="transient_result",
                    delay=delay,
                )
                await asyncio.sleep(delay)
                continue
            return result

        # All retries exhausted; return the last result or raise the last error.
        if last_result is not None:
            return last_result
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"MCP call to {server_id}:{tool_name} failed after retries")

    def _call_timeout(self):
        """Bound a single MCP request, or pass through when unconfigured."""

        if self.call_timeout_seconds is None:
            return contextlib.nullcontext()
        return asyncio.timeout(self.call_timeout_seconds)

    async def call_prompt(self, server_id: str, prompt_name: str, arguments: dict[str, Any]) -> str:
        """Invoke an MCP prompt and serialize the returned prompt messages."""
        session = self.sessions.get(server_id)
        if session is None or session.session is None:
            raise RuntimeError(f'MCP server "{server_id}" is not connected.')
        async with self._call_timeout():
            result = await session.session.get_prompt(prompt_name, arguments)
        return json.dumps(
            result.model_dump(mode="json") if hasattr(result, "model_dump") else str(result),
            ensure_ascii=False,
        )

    async def close_all(self) -> None:
        """释放本 manager 持有的全部 MCP 会话。"""
        if self._session_pool is not None:
            # Pooled sessions outlive the run; returning the lease is what keeps
            # the next turn from paying another cold start.
            for server_id, fingerprint in self._leases.items():
                session = self.sessions.get(server_id)
                # A session whose transport died during the run must not be
                # handed to the next run; retiring it forces a fresh connect.
                healthy = session is not None and session.session is not None
                await self._session_pool.release(fingerprint, healthy=healthy)
            self._leases.clear()
            self.sessions.clear()
            return
        # 逐个关闭并吞掉异常，保证一个关不掉的 server 不会导致其余 server
        # 泄漏子进程。每次 run 结束都会调用，这里漏一个就是持续泄漏。
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


async def load_mcp_manager(
    session_pool: "McpSessionPool | None" = None,
) -> McpClientManager:
    """创建加载好配置的 MCP 客户端管理器。"""
    settings = await get_or_init_settings()
    result = load_scoped_mcp_servers(explicit_config_path=settings.mcp_config_path)
    return McpClientManager(
        [_to_legacy_config(server) for server in result.servers],
        result,
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
        call_timeout_seconds=settings.mcp_call_timeout_seconds,
        max_call_retries=settings.mcp_call_max_retries,
        retry_base_delay_seconds=settings.mcp_call_retry_base_delay_seconds,
        session_pool=session_pool,
    )


def mcp_manager_from_runtime(
    servers: list[dict[str, Any]],
    *,
    log_context: dict[str, Any] | None = None,
    connect_timeout_seconds: float = 60.0,
    call_timeout_seconds: float | None = None,
    max_call_retries: int = 0,
    retry_base_delay_seconds: float = 1.0,
    session_pool: "McpSessionPool | None" = None,
) -> McpClientManager:
    """Build a manager only from access-layer supplied runtime definitions."""
    # 请求级 manager：只认 Access Layer 本次传来的定义，不读磁盘配置。
    # 这是无状态边界的关键——用户本轮没选的 server 不会被连上，
    # 返回的 manager 由调用方负责在 run 结束时 close_all。
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
        call_timeout_seconds=call_timeout_seconds,
        max_call_retries=max_call_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
        session_pool=session_pool,
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
