"""Durability and concurrency tests for the Access Layer Agent Team runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from access_layer.teams.models import SupervisorDecision, TeamCreateInput
from access_layer.teams.runtime import TeamRuntime
from access_layer.teams.store import TeamStore
from backend.home import resolve_managed_path


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
                    "networkAccess": False,
                    "responsibility": "独立审查成果",
                },
            ],
        }
    )


async def _approve_initial_plan(store: TeamStore, team: dict) -> dict:
    """Drive the durable supervisor boundary without invoking a provider in store tests."""

    control = await store.claim_supervisor_job(team["id"])
    assert control is not None
    workers = [agent for agent in team["agents"] if not agent["isSupervisor"]]
    actions: list[dict] = [{"type": "approve_plan", "reason": "初始计划可执行"}]
    if not team["tasks"]:
        actions.extend([
            {
                "type": "create_task",
                "taskKey": "research",
                "title": "先完成调研",
                "description": "形成后续实现所需的约束和输入。",
                "assigneeAgentId": workers[0]["id"],
                "dependsOn": [],
                "priority": 100,
                "reason": "先收敛输入",
            },
            {
                "type": "create_task",
                "taskKey": "implementation",
                "title": "基于调研实施",
                "description": "读取已验收调研成果并完成实现。",
                "assigneeAgentId": workers[1]["id"] if len(workers) > 1 else workers[0]["id"],
                "dependsOn": ["research"],
                "priority": 80,
                "reason": "实现依赖调研 Artifact",
            },
        ])
    for index, task in enumerate(team["tasks"]):
        if task["ownerAgentId"] is None:
            actions.append({
                "type": "assign_task",
                "taskId": task["id"],
                "assigneeAgentId": workers[index % len(workers)]["id"],
                "reason": "职责匹配",
            })
    return await store.apply_supervisor_decision(
        team["id"],
        control["job"]["id"],
        control["supervisor"]["id"],
        SupervisorDecision.model_validate({"summary": "批准初始计划", "actions": actions}),
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
        reviewer = next(agent for agent in team["agents"] if agent["name"] == "审查")
        assert reviewer["networkAccess"] is False
        assert team["tasks"] == []
        assert team["supervisorState"]["triggerType"] == "team.started"

    asyncio.run(scenario())


def test_team_permission_mode_is_persisted(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        payload = _team_payload().model_copy(update={"permission_mode": "full_access"})
        team = await store.create_team(payload, tmp_path)
        assert team["permissionMode"] == "full_access"
        summaries = await store.list_teams()
        assert summaries[0]["permissionMode"] == "full_access"

    asyncio.run(scenario())


def test_supervisor_plan_gate_and_task_claim_are_atomic(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(mode="auto"), tmp_path)

        # Automatic mode starts from the goal, not one synthetic task per member.
        assert team["tasks"] == []
        assert await store.claim_next_task(team["id"]) is None
        team = await _approve_initial_plan(store, team)
        assert len(team["tasks"]) == 2
        research = next(task for task in team["tasks"] if task["title"] == "先完成调研")
        implementation = next(task for task in team["tasks"] if task["title"] == "基于调研实施")
        assert implementation["dependsOn"] == [research["id"]]

        claims = await asyncio.gather(
            *(store.claim_next_task(team["id"]) for _ in range(8))
        )
        claimed = [item for item in claims if item is not None]
        assert len(claimed) == 1
        assert claimed[0]["task"]["id"] == research["id"]

        artifact_id = await store.submit_task(
            team["id"], research["id"], claimed[0]["agent"]["id"], "调研成果"
        )
        control = await store.claim_supervisor_job(team["id"])
        assert control is not None
        await store.apply_supervisor_decision(
            team["id"],
            control["job"]["id"],
            control["supervisor"]["id"],
            SupervisorDecision.model_validate({
                "summary": "调研通过，释放实现任务",
                "actions": [{
                    "type": "accept_submission",
                    "taskId": research["id"],
                    "artifactId": artifact_id,
                    "reason": "输入完整",
                }],
            }),
        )
        next_claim = await store.claim_next_task(team["id"])
        assert next_claim is not None
        assert next_claim["task"]["id"] == implementation["id"]

    asyncio.run(scenario())


def test_automatic_plan_rejects_empty_or_cyclic_task_graph(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(mode="auto"), tmp_path)
        control = await store.claim_supervisor_job(team["id"])
        assert control is not None
        with pytest.raises(ValueError, match="must create at least one task"):
            await store.apply_supervisor_decision(
                team["id"],
                control["job"]["id"],
                control["supervisor"]["id"],
                SupervisorDecision.model_validate({
                    "summary": "空计划",
                    "actions": [{"type": "approve_plan", "reason": "没有任务"}],
                }),
            )

        workers = [agent for agent in team["agents"] if not agent["isSupervisor"]]
        with pytest.raises(ValueError, match="dependency cycle"):
            await store.apply_supervisor_decision(
                team["id"],
                control["job"]["id"],
                control["supervisor"]["id"],
                SupervisorDecision.model_validate({
                    "summary": "循环计划",
                    "actions": [
                        {
                            "type": "create_task",
                            "taskKey": "a",
                            "title": "A",
                            "description": "A 依赖 B",
                            "assigneeAgentId": workers[0]["id"],
                            "dependsOn": ["b"],
                        },
                        {
                            "type": "create_task",
                            "taskKey": "b",
                            "title": "B",
                            "description": "B 依赖 A",
                            "assigneeAgentId": workers[1]["id"],
                            "dependsOn": ["a"],
                        },
                    ],
                }),
            )

    asyncio.run(scenario())


def test_automatic_plan_runs_only_independent_roots_in_parallel(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(mode="auto"), tmp_path)
        control = await store.claim_supervisor_job(team["id"])
        assert control is not None
        workers = [agent for agent in team["agents"] if not agent["isSupervisor"]]
        planned = await store.apply_supervisor_decision(
            team["id"],
            control["job"]["id"],
            control["supervisor"]["id"],
            SupervisorDecision.model_validate({
                "summary": "调研和风险检查并行，综合随后执行",
                "actions": [
                    {
                        "type": "create_task",
                        "taskKey": "research",
                        "title": "调研",
                        "description": "收集事实",
                        "assigneeAgentId": workers[0]["id"],
                        "dependsOn": [],
                    },
                    {
                        "type": "create_task",
                        "taskKey": "risk",
                        "title": "风险检查",
                        "description": "独立识别风险",
                        "assigneeAgentId": workers[1]["id"],
                        "dependsOn": [],
                    },
                    {
                        "type": "create_task",
                        "taskKey": "synthesis",
                        "title": "综合",
                        "description": "汇总两个已验收成果",
                        "assigneeAgentId": workers[0]["id"],
                        "dependsOn": ["research", "risk"],
                    },
                ],
            }),
        )
        synthesis = next(task for task in planned["tasks"] if task["title"] == "综合")
        assert len(synthesis["dependsOn"]) == 2

        claims = await asyncio.gather(
            *(store.claim_next_task(team["id"]) for _ in range(4))
        )
        claimed = [item for item in claims if item is not None]
        assert {item["task"]["title"] for item in claimed} == {"调研", "风险检查"}
        assert "综合" not in {item["task"]["title"] for item in claimed}

    asyncio.run(scenario())


def test_submitted_artifact_requires_supervisor_acceptance(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await _approve_initial_plan(
            store, await store.create_team(_team_payload(), tmp_path)
        )
        claimed = await store.claim_next_task(team["id"])
        assert claimed is not None
        artifact_id = await store.submit_task(
            team["id"], claimed["task"]["id"], claimed["agent"]["id"], "待验收成果"
        )

        assert await store.claim_next_task(team["id"]) is None
        control = await store.claim_supervisor_job(team["id"])
        assert control is not None
        snapshot = await store.apply_supervisor_decision(
            team["id"],
            control["job"]["id"],
            control["supervisor"]["id"],
            SupervisorDecision.model_validate({
                "summary": "成果满足任务要求",
                "actions": [{
                    "type": "accept_submission",
                    "taskId": claimed["task"]["id"],
                    "artifactId": artifact_id,
                    "reason": "交付内容完整",
                }],
            }),
        )

        task = next(item for item in snapshot["tasks"] if item["id"] == claimed["task"]["id"])
        artifact = next(item for item in snapshot["artifacts"] if item["id"] == artifact_id)
        assert task["status"] == "completed"
        assert artifact["status"] == "accepted"
        event_types = [event["type"] for event in await store.events_after(team["id"], 0)]
        assert "task.submitted" in event_types
        assert "supervisor.decision_applied" in event_types
        assert "task.completed" in event_types

    asyncio.run(scenario())


def test_expired_run_lease_is_recovered_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(), tmp_path)
        team = await _approve_initial_plan(store, team)
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


def test_runtime_stop_requeues_without_spending_attempt(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await _approve_initial_plan(
            store, await store.create_team(_team_payload(), tmp_path)
        )
        claimed = await store.claim_next_task(team["id"])
        assert claimed is not None
        assert claimed["task"]["attempt"] == 1

        await store.interrupt_task(
            team["id"], claimed["task"]["id"], claimed["agent"]["id"], "runtime stopped"
        )

        snapshot = await store.get_team(team["id"])
        task = next(item for item in snapshot["tasks"] if item["id"] == claimed["task"]["id"])
        assert task["status"] == "pending"
        assert task["attempt"] == 0
        assert any(
            event["type"] == "run.interrupted"
            for event in await store.events_after(team["id"], 0)
        )

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
        team = await _approve_initial_plan(store, team)
        claimed = await store.claim_next_task(team["id"])
        assert claimed is not None
        task = claimed["task"]
        agent = claimed["agent"]
        runtime = TeamRuntime(
            store=store,
            backend_client=Backend(),  # type: ignore[arg-type]
            runtime_catalog=Catalog(),  # type: ignore[arg-type]
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


def test_task_run_uses_task_directory_without_repository_checkout(tmp_path: Path) -> None:
    """Non-code teams must materialize only task inputs, outputs, logs, and Artifacts."""

    class Backend:
        async def stream(self, _payload, _request_id):
            yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "可交付成果"}

    class Catalog:
        @staticmethod
        def selected_runtime(_mcp_ids, _skill_ids):
            return [], []

    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(), tmp_path)
        team = await _approve_initial_plan(store, team)
        claimed = await store.claim_next_task(team["id"])
        assert claimed is not None
        runtime = TeamRuntime(
            store=store,
            backend_client=Backend(),  # type: ignore[arg-type]
            runtime_catalog=Catalog(),  # type: ignore[arg-type]
            enabled=False,
        )

        await runtime._run_claimed(claimed)

        task_id = claimed["task"]["id"]
        task_dir = tmp_path / team["id"] / "tasks" / task_id
        manifest = (task_dir / "manifest.json").read_text(encoding="utf-8")
        assert '"status": "submitted"' in manifest
        assert (task_dir / "input" / "task.json").is_file()
        assert (task_dir / "input" / "mailbox.json").is_file()
        assert list((task_dir / "artifacts").glob("artifact_*.md"))
        assert list((task_dir / "logs").glob("teamrun_*.ndjson"))
        assert not (tmp_path / team["id"] / "workspaces").exists()
        assert not (task_dir / "output" / ".git").exists()

        snapshot = await store.get_team(team["id"])
        current_task = next(item for item in snapshot["tasks"] if item["id"] == task_id)
        assert current_task["status"] == "submitted"
        assert snapshot["artifacts"][0]["status"] == "pending_review"

    asyncio.run(scenario())


def test_supervisor_runtime_applies_structured_decision(tmp_path: Path) -> None:
    class Backend:
        def __init__(self, engineer_id: str) -> None:
            self._engineer_id = engineer_id

        async def stream(self, _payload, _request_id):
            # Initial team.started plans must create runnable work; approve_plan alone is rejected.
            yield {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": "manager",
                "delta": json.dumps({
                    "summary": "初始任务与成员职责匹配",
                    "actions": [
                        {
                            "type": "approve_plan",
                            "reason": "允许成员开始执行",
                        },
                        {
                            "type": "create_task",
                            "taskKey": "implement",
                            "title": "实现核心能力",
                            "description": "按目标完成可审查交付。",
                            "assigneeAgentId": self._engineer_id,
                            "dependsOn": [],
                            "priority": 100,
                            "reason": "职责匹配",
                        },
                    ],
                }, ensure_ascii=False),
            }

    class Catalog:
        pass

    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        team = await store.create_team(_team_payload(), tmp_path)
        engineer = next(agent for agent in team["agents"] if agent["role"] == "Engineer")
        control = await store.claim_supervisor_job(team["id"])
        assert control is not None
        runtime = TeamRuntime(
            store=store,
            backend_client=Backend(engineer["id"]),  # type: ignore[arg-type]
            runtime_catalog=Catalog(),  # type: ignore[arg-type]
            enabled=False,
        )

        await runtime._run_supervisor(control)

        snapshot = await store.get_team(team["id"])
        assert snapshot["supervisorState"]["status"] == "completed"
        assert await store.claim_next_task(team["id"]) is not None
        decision_file = (
            tmp_path / team["id"] / "supervisor" / control["job"]["id"] / "decision.json"
        )
        assert decision_file.is_file()
        assert json.loads(decision_file.read_text(encoding="utf-8"))["actions"][0]["type"] == "approve_plan"

    asyncio.run(scenario())


def test_accepted_files_publish_to_team_workspace_and_flow_downstream(tmp_path: Path) -> None:
    import os
    from unittest.mock import patch

    from access_layer.home import reset_home_cache, shared_runtime_dir

    workspace = tmp_path / "team-deliverables"
    home = tmp_path / "k_agent_home"

    class WorkerBackend:
        async def stream(self, payload, _request_id):
            root = Path(payload["workspaceDir"])
            output = root / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "site.html").write_text("<main>accepted</main>", encoding="utf-8")
            yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "worker", "delta": "站点交付"}

    class SupervisorBackend:
        def __init__(self, task_id: str, assignee_id: str) -> None:
            self.task_id = task_id
            self.assignee_id = assignee_id

        async def stream(self, _payload, _request_id):
            yield {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": "supervisor",
                "delta": json.dumps({
                    "summary": "验收站点并安排下游检查",
                    "actions": [
                        {
                            "type": "accept_submission",
                            "taskId": self.task_id,
                            "reason": "文件和说明完整",
                        },
                        {
                            "type": "create_task",
                            "taskKey": "downstream_check",
                            "title": "检查已发布站点",
                            "description": "读取上游已验收文件并检查。",
                            "assigneeAgentId": self.assignee_id,
                            "dependsOn": [self.task_id],
                            "priority": 100,
                            "reason": "由下游成员读取正式 Artifact",
                        },
                    ],
                }, ensure_ascii=False),
            }

    class Catalog:
        @staticmethod
        def selected_runtime(_mcp_ids, _skill_ids):
            return [], []

    async def scenario() -> None:
        store = TeamStore(tmp_path / "team_runtime.db")
        await store.initialize()
        payload = _team_payload().model_copy(update={"workspace_dir": str(workspace)})
        team = await _approve_initial_plan(store, await store.create_team(payload, tmp_path))
        claimed = await store.claim_next_task(team["id"])
        assert claimed is not None
        worker_runtime = TeamRuntime(
            store=store,
            backend_client=WorkerBackend(),  # type: ignore[arg-type]
            runtime_catalog=Catalog(),  # type: ignore[arg-type]
            enabled=False,
        )
        await worker_runtime._run_claimed(claimed)

        task_runtime = tmp_path / team["id"] / "tasks" / claimed["task"]["id"] / ".runtime"
        project_runtime = shared_runtime_dir()
        assert task_runtime.is_symlink()
        assert task_runtime.resolve() == project_runtime.resolve()
        assert (project_runtime / "node").is_dir()

        control = await store.claim_supervisor_job(team["id"])
        assert control is not None
        downstream_agent = next(
            agent for agent in team["agents"] if agent["id"] != claimed["agent"]["id"] and not agent["isSupervisor"]
        )
        supervisor_runtime = TeamRuntime(
            store=store,
            backend_client=SupervisorBackend(claimed["task"]["id"], downstream_agent["id"]),  # type: ignore[arg-type]
            runtime_catalog=Catalog(),  # type: ignore[arg-type]
            enabled=False,
        )
        await supervisor_runtime._run_supervisor(control)

        snapshot = await store.get_team(team["id"])
        artifact = next(item for item in snapshot["artifacts"] if item["taskId"] == claimed["task"]["id"])
        published = resolve_managed_path(artifact["workspacePath"])
        assert snapshot["workspaceDir"] == str(workspace.resolve())
        assert (workspace / ".k_agent-team.json").is_file()
        assert (published / "files" / "site.html").read_text(encoding="utf-8") == "<main>accepted</main>"
        assert (published / "result.md").read_text(encoding="utf-8") == "站点交付"

        downstream = await store.claim_next_task(team["id"])
        assert downstream is not None
        assert downstream["task"]["title"] == "检查已发布站点"
        context = await store.task_context(team["id"], downstream["task"]["id"])
        downstream_dir = tmp_path / team["id"] / "tasks" / downstream["task"]["id"]
        supervisor_runtime._prepare_task_directory(
            downstream_dir,
            context,
            downstream["agent"],
            downstream["runId"],
            runtime=project_runtime,
        )
        copied = downstream_dir / "output" / ".team-input" / "artifacts" / artifact["id"] / "files" / "site.html"
        assert copied.read_text(encoding="utf-8") == "<main>accepted</main>"
        assert not (
            downstream_dir / "output" / ".team-input" / "artifacts" / artifact["id"] / "files" / "node_modules"
        ).exists()

    with patch.dict(os.environ, {"K_AGENT_HOME": str(home)}, clear=False):
        reset_home_cache()
        try:
            asyncio.run(scenario())
        finally:
            reset_home_cache()
