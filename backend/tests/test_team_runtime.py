"""Durability and concurrency tests for the Access Layer Agent Team runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from access_layer.teams.models import TeamCreateInput
from access_layer.teams.runtime import TeamRuntime
from access_layer.teams.store import TeamStore


def _team_payload(*, mode: str = "manual") -> TeamCreateInput:
    return TeamCreateInput.model_validate(
        {
            "name": "三运行时团队",
            "goal": "验证 Team Runtime",
            "mode": mode,
            "agents": [
                {
                    "name": "主管",
                    "role": "Supervisor",
                    "agentKind": "k_agent",
                    "isSupervisor": True,
                },
                {
                    "name": "实现",
                    "role": "Engineer",
                    "agentKind": "codex",
                    "responsibility": "实现并验证功能",
                },
                {
                    "name": "审查",
                    "role": "Reviewer",
                    "agentKind": "claude_code",
                    "responsibility": "独立审查成果",
                },
            ],
        }
    )


def test_team_store_registers_all_builtin_agent_kinds(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(), tmp_path)

        assert {agent["agentKind"] for agent in team["agents"]} == {
            "k_agent",
            "codex",
            "claude_code",
        }
        assert sum(agent["isSupervisor"] for agent in team["agents"]) == 1
        assert team["tasks"][-1]["taskType"] in {"work", "synthesis"}

    asyncio.run(scenario())


def test_task_claim_is_atomic_and_dependencies_gate_synthesis(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(mode="auto"), tmp_path)

        claims = await asyncio.gather(
            *(store.claim_next_task(team["id"]) for _ in range(8))
        )
        claimed = [item for item in claims if item is not None]
        # Two independent Worker tasks can run, while synthesis remains blocked
        # by its dependencies and the supervisor remains idle.
        assert len(claimed) == 2
        assert len({item["task"]["id"] for item in claimed}) == 2
        assert all(item["task"]["taskType"] == "work" for item in claimed)

        for item in claimed:
            await store.complete_task(
                team["id"], item["task"]["id"], item["agent"]["id"], "成果"
            )
        synthesis = await store.claim_next_task(team["id"])
        assert synthesis is not None
        assert synthesis["task"]["taskType"] == "synthesis"

    asyncio.run(scenario())


def test_expired_run_lease_is_recovered_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(), tmp_path)
        claimed = await store.claim_next_task(team["id"])
        assert claimed is not None

        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(store.database_path) as db:
            db.execute(
                "UPDATE team_tasks SET lease_until=? WHERE id=?",
                (expired, claimed["task"]["id"]),
            )
        assert await store.recover_expired_leases() == 1
        recovered = await store.get_team(team["id"])
        task = next(item for item in recovered["tasks"] if item["id"] == claimed["task"]["id"])
        agent = next(item for item in recovered["agents"] if item["id"] == claimed["agent"]["id"])
        assert task["status"] == "pending"
        assert agent["status"] == "idle"
        assert any(event["type"] == "run.recovered" for event in await store.events_after(team["id"], 0))

    asyncio.run(scenario())


def test_agent_stream_persists_batched_output_reasoning_and_tools(tmp_path: Path) -> None:
    """The workbench must be able to replay an Agent run before its Artifact exists."""

    class Backend:
        async def stream(self, _payload, _request_id):
            for event in [
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "r1", "delta": "先检查依赖。"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "正在实现"},
                {"type": "TOOL_CALL_START", "toolCallId": "t1", "toolCallName": "shell"},
                {"type": "TOOL_CALL_RESULT", "toolCallId": "t1", "content": "ok"},
                {"type": "CUSTOM", "name": "approval_request", "value": {
                    "id": "approval_1", "threadId": "team:agent", "runId": "run_test",
                    "agentKind": "codex", "category": "command",
                    "title": "允许执行命令？", "message": "需要确认",
                    "detail": {"command": "npm test"},
                }},
                {"type": "CUSTOM", "name": "approval_resolved", "value": {
                    "id": "approval_1", "threadId": "team:agent", "runId": "run_test",
                    "action": "approve",
                }},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "，随后验证。"},
            ]:
                yield event

    class Catalog:
        pass

    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(), tmp_path)
        claimed = await store.claim_next_task(team["id"])
        assert claimed is not None
        task = claimed["task"]
        agent = claimed["agent"]
        runtime = TeamRuntime(
            store=store,
            backend_client=Backend(),  # type: ignore[arg-type]
            runtime_catalog=Catalog(),  # type: ignore[arg-type]
            project_root=tmp_path,
            enabled=False,
        )
        text = await runtime._stream_agent(
            team_id=team["id"], task_id=task["id"], agent_id=agent["id"],
            run_id="run_test", request_id="request_test", prompt="test", agent=agent,
            mcp_servers=[], skills=[], workspace=tmp_path,
        )
        events = await store.events_after(team["id"], 0)

        assert text == "正在实现，随后验证。"
        assert "".join(event["payload"].get("delta", "") for event in events if event["type"] == "run.output") == text
        assert "".join(event["payload"].get("delta", "") for event in events if event["type"] == "run.reasoning") == "先检查依赖。"
        assert [event["payload"]["event"]["type"] for event in events if event["type"] == "run.activity"] == ["TOOL_CALL_START", "TOOL_CALL_RESULT"]
        assert [event["type"] for event in events if event["type"].startswith("approval.")] == [
            "approval.requested", "approval.resolved",
        ]
        snapshot = await store.get_team(team["id"])
        current_agent = next(item for item in snapshot["agents"] if item["id"] == agent["id"])
        assert current_agent["status"] == "busy"

    asyncio.run(scenario())
