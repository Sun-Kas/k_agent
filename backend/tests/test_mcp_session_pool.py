from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.mcp_tool.client import McpClientManager, McpServerConfig
from backend.mcp_tool.pool import McpSessionPool, fingerprint_config


def server(identifier: str = "srv", *, args: list[str] | None = None) -> McpServerConfig:
    return McpServerConfig(
        id=identifier,
        scope="local",
        type="stdio",
        command="uvx",
        args=args if args is not None else ["example-mcp"],
        env={},
    )


def fake_session() -> SimpleNamespace:
    """A session stub that reports itself connected after connect()."""

    stub = SimpleNamespace(session=None, close=AsyncMock())

    async def connect() -> None:
        stub.session = object()

    stub.connect = AsyncMock(side_effect=connect)
    return stub


class SessionPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_configuration_reuses_one_connection(self) -> None:
        pool = McpSessionPool()
        stub = fake_session()
        with patch("backend.mcp_tool.pool.McpSession", return_value=stub):
            first, _ = await pool.acquire(server(), connect_timeout_seconds=5)
            await pool.release(first)
            second, _ = await pool.acquire(server(), connect_timeout_seconds=5)
            await pool.release(second)
        self.assertEqual(first, second)
        self.assertEqual(stub.connect.await_count, 1)
        await pool.close_all(force=True)

    async def test_changed_configuration_gets_a_separate_connection(self) -> None:
        self.assertNotEqual(
            fingerprint_config(server(args=["a"])),
            fingerprint_config(server(args=["b"])),
        )

    async def test_failed_connect_is_not_left_in_the_pool(self) -> None:
        pool = McpSessionPool()
        stub = SimpleNamespace(
            session=None,
            connect=AsyncMock(side_effect=RuntimeError("cold start failed")),
            close=AsyncMock(),
        )
        with patch("backend.mcp_tool.pool.McpSession", return_value=stub):
            with self.assertRaises(RuntimeError):
                await pool.acquire(server(), connect_timeout_seconds=5)
        self.assertEqual(await pool.stats(), {"pooledSessions": 0, "leasedSessions": 0})

    async def test_concurrent_runs_share_a_single_cold_start(self) -> None:
        pool = McpSessionPool()
        stub = SimpleNamespace(session=None, close=AsyncMock())
        started = asyncio.Event()

        async def connect() -> None:
            started.set()
            await asyncio.sleep(0.01)
            stub.session = object()

        stub.connect = AsyncMock(side_effect=connect)
        with patch("backend.mcp_tool.pool.McpSession", return_value=stub):
            leases = await asyncio.gather(
                pool.acquire(server(), connect_timeout_seconds=5),
                pool.acquire(server(), connect_timeout_seconds=5),
            )
        self.assertTrue(started.is_set())
        self.assertEqual(stub.connect.await_count, 1)
        for fingerprint, _ in leases:
            await pool.release(fingerprint)
        await pool.close_all(force=True)

    async def test_leased_session_survives_a_run_but_closes_on_shutdown(self) -> None:
        pool = McpSessionPool()
        stub = fake_session()
        with patch("backend.mcp_tool.pool.McpSession", return_value=stub):
            manager = McpClientManager([server()], session_pool=pool)
            await manager.connect_all()
            await manager.close_all()
        stub.close.assert_not_awaited()
        self.assertEqual((await pool.stats())["pooledSessions"], 1)
        await pool.close_all(force=True)
        stub.close.assert_awaited_once()

    async def test_dead_session_is_retired_instead_of_reused(self) -> None:
        pool = McpSessionPool()
        stub = fake_session()
        with patch("backend.mcp_tool.pool.McpSession", return_value=stub):
            manager = McpClientManager([server()], session_pool=pool)
            await manager.connect_all()
            # The transport died mid-run; McpSession clears .session on exit.
            stub.session = None
            await manager.close_all()
        self.assertEqual(await pool.stats(), {"pooledSessions": 0, "leasedSessions": 0})
        stub.close.assert_awaited_once()

    async def test_idle_sessions_are_evicted_after_their_ttl(self) -> None:
        pool = McpSessionPool(idle_ttl_seconds=0)
        stub = fake_session()
        replacement = fake_session()
        with patch("backend.mcp_tool.pool.McpSession", return_value=stub):
            fingerprint, _ = await pool.acquire(server(), connect_timeout_seconds=5)
            await pool.release(fingerprint)
        with patch("backend.mcp_tool.pool.McpSession", return_value=replacement):
            await pool.acquire(server(), connect_timeout_seconds=5)
        stub.close.assert_awaited_once()
        await pool.close_all(force=True)


if __name__ == "__main__":
    unittest.main()
