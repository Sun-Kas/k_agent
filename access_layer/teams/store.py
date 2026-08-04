"""Transactional SQLite store for the local-first Agent Team control plane."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from access_layer.teams.models import TeamCreateInput, TeamTaskCreateInput


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamStore:
    """Persist Team state with transactions instead of process-local file locks."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the WAL database and idempotent schema."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS teams (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    max_parallel INTEGER NOT NULL,
                    supervisor_agent_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS team_agents (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    agent_kind TEXT NOT NULL,
                    model_id TEXT,
                    reasoning_effort TEXT,
                    responsibility TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_supervisor INTEGER NOT NULL,
                    creation_reason TEXT NOT NULL,
                    capability_json TEXT NOT NULL,
                    workspace_dir TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS team_tasks (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    owner_agent_id TEXT REFERENCES team_agents(id),
                    depends_on_json TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_until TEXT,
                    run_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL REFERENCES team_tasks(id) ON DELETE CASCADE,
                    agent_id TEXT REFERENCES team_agents(id),
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mailbox_messages (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT
                );
                CREATE TABLE IF NOT EXISTS team_events (
                    event_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(team_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_dispatch
                    ON team_tasks(team_id, status, priority, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_replay
                    ON team_events(team_id, seq);
                CREATE INDEX IF NOT EXISTS idx_mail_recipient
                    ON mailbox_messages(team_id, recipient_id, status, created_at);
                """
            )

    async def create_team(self, payload: TeamCreateInput, teams_root: Path) -> dict[str, Any]:
        """Create members and initial tasks in one transaction."""

        async with self._write_lock:
            return await asyncio.to_thread(self._create_team_sync, payload, teams_root)

    def _create_team_sync(self, payload: TeamCreateInput, teams_root: Path) -> dict[str, Any]:
        team_id = f"team_{uuid.uuid4().hex}"
        timestamp = _now()
        agent_ids = [f"agent_{uuid.uuid4().hex}" for _ in payload.agents]
        supervisor_index = next(
            (index for index, agent in enumerate(payload.agents) if agent.is_supervisor),
            0,
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO teams VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (
                    team_id,
                    payload.name,
                    payload.goal,
                    payload.mode,
                    payload.max_parallel,
                    agent_ids[supervisor_index],
                    timestamp,
                    timestamp,
                ),
            )
            for index, (agent_id, agent) in enumerate(zip(agent_ids, payload.agents)):
                capability = {
                    "mcpServerIds": agent.mcp_server_ids,
                    "skillIds": agent.skill_ids,
                }
                workspace = teams_root / team_id / "workspaces" / agent_id
                reason = (
                    "用户指定为团队主管"
                    if index == supervisor_index
                    else agent.responsibility or f"负责 {agent.role} 能力范围内的任务"
                )
                db.execute(
                    """
                    INSERT INTO team_agents VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        team_id,
                        agent.name,
                        agent.role,
                        agent.agent_kind,
                        agent.model_id,
                        agent.reasoning_effort,
                        agent.responsibility,
                        1 if index == supervisor_index else 0,
                        reason,
                        json.dumps(capability, ensure_ascii=False),
                        str(workspace),
                        timestamp,
                        timestamp,
                    ),
                )

            task_specs = list(payload.tasks)
            task_ids: list[str] = []
            if task_specs:
                task_ids = [f"task_{uuid.uuid4().hex}" for _ in task_specs]
                for index, (task_id, task) in enumerate(zip(task_ids, task_specs)):
                    owner_index = task.assigned_agent_index
                    if owner_index is not None and owner_index >= len(agent_ids):
                        raise ValueError(f"Task {index + 1} assignedAgentIndex is out of range")
                    dependencies = [
                        task_ids[item]
                        for item in task.depends_on
                        if 0 <= item < len(task_ids) and item != index
                    ]
                    db.execute(
                        """
                        INSERT INTO team_tasks VALUES
                        (?, ?, ?, ?, 'work', 'pending', ?, ?, ?, 0, 3, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            task_id,
                            team_id,
                            task.title,
                            task.description,
                            task.priority,
                            agent_ids[owner_index] if owner_index is not None else None,
                            json.dumps(dependencies),
                            timestamp,
                            timestamp,
                        ),
                    )
            else:
                worker_indices = [index for index in range(len(agent_ids)) if index != supervisor_index]
                if not worker_indices:
                    worker_indices = [supervisor_index]
                for index in worker_indices:
                    task_id = f"task_{uuid.uuid4().hex}"
                    task_ids.append(task_id)
                    agent = payload.agents[index]
                    description = agent.responsibility.strip() or (
                        f"以 {agent.role} 身份完成团队目标中适合你的部分，并提交可验证成果。"
                    )
                    db.execute(
                        """
                        INSERT INTO team_tasks VALUES
                        (?, ?, ?, ?, 'work', 'pending', 60, ?, '[]', 0, 3, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            task_id,
                            team_id,
                            f"{agent.role}交付",
                            description,
                            # Auto mode leaves Worker tasks unowned so the
                            # scheduler performs a transactional capability/idle
                            # claim instead of treating the draft as assignment.
                            None if payload.mode == "auto" else agent_ids[index],
                            timestamp,
                            timestamp,
                        ),
                    )

                if len(payload.agents) > 1:
                    synthesis_id = f"task_{uuid.uuid4().hex}"
                    db.execute(
                        """
                        INSERT INTO team_tasks VALUES
                        (?, ?, '主管综合与质量审查', ?, 'synthesis', 'pending', 20, ?, ?, 0, 3, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            synthesis_id,
                            team_id,
                            "审查所有 Worker Artifact，指出冲突或遗漏，并形成面向用户的最终成果。",
                            agent_ids[supervisor_index],
                            json.dumps(task_ids),
                            timestamp,
                            timestamp,
                        ),
                    )
            self._append_event_sync(
                db,
                team_id,
                "team.created",
                {"name": payload.name, "mode": payload.mode, "agentCount": len(agent_ids)},
            )
            db.commit()
        return self._team_snapshot_sync(team_id)

    def _append_event_sync(
        self, db: sqlite3.Connection, team_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM team_events WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        seq = int(row["next_seq"])
        event_id = f"event_{uuid.uuid4().hex}"
        occurred_at = _now()
        db.execute(
            "INSERT INTO team_events VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, team_id, seq, event_type, json.dumps(payload, ensure_ascii=False), occurred_at),
        )
        return {
            "teamId": team_id,
            "seq": seq,
            "eventId": event_id,
            "type": event_type,
            "payload": payload,
            "occurredAt": occurred_at,
        }

    async def append_event(self, team_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._write_lock:
            return await asyncio.to_thread(self._append_event_transaction, team_id, event_type, payload)

    def _append_event_transaction(self, team_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            event = self._append_event_sync(db, team_id, event_type, payload)
            db.commit()
            return event

    async def record_approval(
        self,
        team_id: str,
        task_id: str,
        agent_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an approval transition and its Agent wait state atomically."""

        async with self._write_lock:
            return await asyncio.to_thread(
                self._record_approval_sync,
                team_id,
                task_id,
                agent_id,
                event_type,
                payload,
            )

    def _record_approval_sync(
        self,
        team_id: str,
        task_id: str,
        agent_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if event_type not in {"approval.requested", "approval.resolved"}:
            raise ValueError(f"Unsupported approval event: {event_type}")
        timestamp = _now()
        target_status = "waiting" if event_type == "approval.requested" else "busy"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Scope the status change to the task's current owner. A delayed
            # resolution from an old attempt must not wake an unrelated run.
            db.execute(
                """
                UPDATE team_agents SET status=?, updated_at=?
                WHERE id=? AND team_id=? AND EXISTS (
                    SELECT 1 FROM team_tasks
                    WHERE id=? AND team_id=? AND owner_agent_id=? AND status='running'
                )
                """,
                (
                    target_status,
                    timestamp,
                    agent_id,
                    team_id,
                    task_id,
                    team_id,
                    agent_id,
                ),
            )
            db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))
            event = self._append_event_sync(db, team_id, event_type, payload)
            db.commit()
            return event

    async def list_teams(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_teams_sync)

    def _list_teams_sync(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT t.*,
                    (SELECT COUNT(*) FROM team_agents a WHERE a.team_id=t.id) agent_count,
                    (SELECT COUNT(*) FROM team_tasks k WHERE k.team_id=t.id) task_count,
                    (SELECT COUNT(*) FROM team_tasks k WHERE k.team_id=t.id AND k.status='completed') completed_count
                FROM teams t ORDER BY t.updated_at DESC
                """
            ).fetchall()
            return [
                {
                    "id": row["id"], "name": row["name"], "goal": row["goal"],
                    "mode": row["mode"], "status": row["status"],
                    "agentCount": row["agent_count"], "taskCount": row["task_count"],
                    "completedTaskCount": row["completed_count"],
                    "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                }
                for row in rows
            ]

    async def get_team(self, team_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._team_snapshot_sync, team_id)

    def _team_snapshot_sync(self, team_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            team = db.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
            if team is None:
                return None
            agents = db.execute(
                "SELECT * FROM team_agents WHERE team_id=? ORDER BY is_supervisor DESC, created_at",
                (team_id,),
            ).fetchall()
            tasks = db.execute(
                "SELECT * FROM team_tasks WHERE team_id=? ORDER BY priority DESC, created_at",
                (team_id,),
            ).fetchall()
            artifacts = db.execute(
                "SELECT * FROM artifacts WHERE team_id=? ORDER BY created_at DESC", (team_id,)
            ).fetchall()
            mailbox = db.execute(
                "SELECT * FROM mailbox_messages WHERE team_id=? ORDER BY created_at DESC LIMIT 100",
                (team_id,),
            ).fetchall()
            last_seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) value FROM team_events WHERE team_id=?", (team_id,)
            ).fetchone()["value"]
            return {
                "id": team["id"], "name": team["name"], "goal": team["goal"],
                "mode": team["mode"], "status": team["status"],
                "maxParallel": team["max_parallel"],
                "supervisorAgentId": team["supervisor_agent_id"],
                "createdAt": team["created_at"], "updatedAt": team["updated_at"],
                "lastEventSeq": last_seq,
                "agents": [self._agent_dict(row) for row in agents],
                "tasks": [self._task_dict(row) for row in tasks],
                "artifacts": [self._artifact_dict(row) for row in artifacts],
                "mailbox": [self._mail_dict(row) for row in mailbox],
            }

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "teamId": row["team_id"], "name": row["name"],
            "role": row["role"], "agentKind": row["agent_kind"],
            "modelId": row["model_id"], "reasoningEffort": row["reasoning_effort"],
            "responsibility": row["responsibility"], "status": row["status"],
            "isSupervisor": bool(row["is_supervisor"]),
            "creationReason": row["creation_reason"],
            "capabilities": json.loads(row["capability_json"]),
            "workspaceDir": row["workspace_dir"], "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "teamId": row["team_id"], "title": row["title"],
            "description": row["description"], "taskType": row["task_type"],
            "status": row["status"], "priority": row["priority"],
            "ownerAgentId": row["owner_agent_id"],
            "dependsOn": json.loads(row["depends_on_json"]),
            "attempt": row["attempt"], "maxAttempts": row["max_attempts"],
            "leaseUntil": row["lease_until"], "runId": row["run_id"],
            "error": row["error"], "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _artifact_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "teamId": row["team_id"], "taskId": row["task_id"],
            "agentId": row["agent_id"], "kind": row["kind"], "title": row["title"],
            "content": row["content"], "sha256": row["sha256"],
            "version": row["version"], "status": row["status"],
            "createdAt": row["created_at"],
            "uri": f"artifact://{row['team_id']}/{row['id']}@{row['version']}",
        }

    @staticmethod
    def _mail_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "senderId": row["sender_id"],
            "recipientId": row["recipient_id"], "messageType": row["message_type"],
            "content": row["content"], "artifactIds": json.loads(row["artifact_ids_json"]),
            "status": row["status"], "createdAt": row["created_at"],
        }

    async def events_after(self, team_id: str, seq: int, limit: int = 200) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._events_after_sync, team_id, seq, limit)

    def _events_after_sync(self, team_id: str, seq: int, limit: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM team_events WHERE team_id=? AND seq>? ORDER BY seq LIMIT ?",
                (team_id, seq, limit),
            ).fetchall()
            return [
                {
                    "teamId": row["team_id"], "seq": row["seq"],
                    "eventId": row["event_id"], "type": row["type"],
                    "payload": json.loads(row["payload_json"]),
                    "occurredAt": row["occurred_at"],
                }
                for row in rows
            ]

    async def command(self, team_id: str, command: str) -> dict[str, Any] | None:
        async with self._write_lock:
            return await asyncio.to_thread(self._command_sync, team_id, command)

    def _command_sync(self, team_id: str, command: str) -> dict[str, Any] | None:
        target = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[command]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM teams WHERE id=?", (team_id,)).fetchone()
            if row is None:
                return None
            timestamp = _now()
            db.execute("UPDATE teams SET status=?, updated_at=? WHERE id=?", (target, timestamp, team_id))
            if command == "cancel":
                db.execute(
                    "UPDATE team_tasks SET status='cancelled', updated_at=? WHERE team_id=? AND status IN ('pending','ready','claimed')",
                    (timestamp, team_id),
                )
            self._append_event_sync(db, team_id, f"team.{target}", {"previousStatus": row["status"]})
            db.commit()
        return self._team_snapshot_sync(team_id)

    async def create_task(self, team_id: str, payload: TeamTaskCreateInput) -> dict[str, Any] | None:
        async with self._write_lock:
            return await asyncio.to_thread(self._create_task_sync, team_id, payload)

    def _create_task_sync(self, team_id: str, payload: TeamTaskCreateInput) -> dict[str, Any] | None:
        task_id = f"task_{uuid.uuid4().hex}"
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone() is None:
                return None
            db.execute(
                """
                INSERT INTO team_tasks VALUES
                (?, ?, ?, ?, ?, 'pending', ?, ?, ?, 0, 3, NULL, NULL, NULL, ?, ?)
                """,
                (task_id, team_id, payload.title, payload.description, payload.task_type,
                 payload.priority, payload.owner_agent_id, json.dumps(payload.depends_on), timestamp, timestamp),
            )
            self._append_event_sync(db, team_id, "task.created", {"taskId": task_id, "title": payload.title})
            db.commit()
        return self._team_snapshot_sync(team_id)

    async def send_message(
        self, team_id: str, sender_id: str, recipient_id: str, message_type: str,
        content: str, artifact_ids: list[str]
    ) -> dict[str, Any] | None:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._send_message_sync, team_id, sender_id, recipient_id,
                message_type, content, artifact_ids,
            )

    def _send_message_sync(self, team_id: str, sender_id: str, recipient_id: str,
                           message_type: str, content: str, artifact_ids: list[str]) -> dict[str, Any] | None:
        message_id = f"mail_{uuid.uuid4().hex}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone() is None:
                return None
            db.execute(
                "INSERT INTO mailbox_messages VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', ?, NULL)",
                (message_id, team_id, sender_id, recipient_id, message_type, content,
                 json.dumps(artifact_ids), _now()),
            )
            self._append_event_sync(db, team_id, "mail.sent", {
                "messageId": message_id, "senderId": sender_id,
                "recipientId": recipient_id, "messageType": message_type,
            })
            db.commit()
        return self._team_snapshot_sync(team_id)

    async def claim_next_task(self, team_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        """Atomically claim one dependency-ready task whose owner is idle."""

        async with self._write_lock:
            return await asyncio.to_thread(self._claim_next_task_sync, team_id, lease_seconds)

    def _claim_next_task_sync(self, team_id: str, lease_seconds: int) -> dict[str, Any] | None:
        timestamp = _now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            team = db.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
            if team is None or team["status"] != "running":
                return None
            active_count = db.execute(
                "SELECT COUNT(*) count FROM team_tasks WHERE team_id=? AND status IN ('claimed','running')",
                (team_id,),
            ).fetchone()["count"]
            if active_count >= team["max_parallel"]:
                return None
            rows = db.execute(
                "SELECT * FROM team_tasks WHERE team_id=? AND status IN ('pending','ready') ORDER BY priority DESC, created_at",
                (team_id,),
            ).fetchall()
            selected = None
            for row in rows:
                dependencies = json.loads(row["depends_on_json"])
                if dependencies:
                    placeholders = ",".join("?" for _ in dependencies)
                    complete = db.execute(
                        f"SELECT COUNT(*) count FROM team_tasks WHERE id IN ({placeholders}) AND status='completed'",
                        dependencies,
                    ).fetchone()["count"]
                    if complete != len(dependencies):
                        continue
                owner_id = row["owner_agent_id"]
                if owner_id is None:
                    owner = db.execute(
                        "SELECT * FROM team_agents WHERE team_id=? AND status='idle' ORDER BY is_supervisor, created_at LIMIT 1",
                        (team_id,),
                    ).fetchone()
                else:
                    owner = db.execute(
                        "SELECT * FROM team_agents WHERE id=? AND status='idle'", (owner_id,)
                    ).fetchone()
                if owner is not None:
                    selected = (row, owner)
                    break
            if selected is None:
                return None
            task, agent = selected
            run_id = f"teamrun_{uuid.uuid4().hex}"
            attempt = int(task["attempt"]) + 1
            db.execute(
                "UPDATE team_tasks SET status='running', owner_agent_id=?, attempt=?, lease_until=?, run_id=?, error=NULL, updated_at=? WHERE id=?",
                (agent["id"], attempt, lease_until, run_id, timestamp, task["id"]),
            )
            db.execute(
                "UPDATE team_agents SET status='busy', updated_at=? WHERE id=?",
                (timestamp, agent["id"]),
            )
            self._append_event_sync(db, team_id, "task.claimed", {
                "taskId": task["id"], "agentId": agent["id"],
                "runId": run_id, "attempt": attempt,
            })
            db.commit()
            return {
                "team": dict(team), "task": self._task_dict(db.execute("SELECT * FROM team_tasks WHERE id=?", (task["id"],)).fetchone()),
                "agent": self._agent_dict(agent), "runId": run_id,
            }

    async def task_context(self, team_id: str, task_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._task_context_sync, team_id, task_id)

    def _task_context_sync(self, team_id: str, task_id: str) -> dict[str, Any]:
        with self._connect() as db:
            team = dict(db.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone())
            task = db.execute("SELECT * FROM team_tasks WHERE id=?", (task_id,)).fetchone()
            dependencies = json.loads(task["depends_on_json"])
            artifacts: list[dict[str, Any]] = []
            if dependencies:
                placeholders = ",".join("?" for _ in dependencies)
                rows = db.execute(
                    f"SELECT * FROM artifacts WHERE task_id IN ({placeholders}) AND status='accepted' ORDER BY created_at",
                    dependencies,
                ).fetchall()
                artifacts = [self._artifact_dict(row) for row in rows]
            mailbox_rows = db.execute(
                "SELECT * FROM mailbox_messages WHERE team_id=? AND recipient_id IN (?, 'broadcast') AND status='sent' ORDER BY created_at",
                (team_id, task["owner_agent_id"]),
            ).fetchall()
            return {"team": team, "task": self._task_dict(task), "artifacts": artifacts,
                    "mailbox": [self._mail_dict(row) for row in mailbox_rows]}

    async def complete_task(self, team_id: str, task_id: str, agent_id: str, content: str) -> str:
        async with self._write_lock:
            return await asyncio.to_thread(self._complete_task_sync, team_id, task_id, agent_id, content)

    def _complete_task_sync(self, team_id: str, task_id: str, agent_id: str, content: str) -> str:
        import hashlib
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM team_tasks WHERE id=?", (task_id,)).fetchone()
            kind = "final_answer" if task["task_type"] == "synthesis" else "report"
            db.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'accepted', ?)",
                (artifact_id, team_id, task_id, agent_id, kind, task["title"], content,
                 hashlib.sha256(content.encode("utf-8")).hexdigest(), timestamp),
            )
            db.execute(
                "UPDATE team_tasks SET status='completed', lease_until=NULL, updated_at=? WHERE id=?",
                (timestamp, task_id),
            )
            db.execute("UPDATE team_agents SET status='idle', updated_at=? WHERE id=?", (timestamp, agent_id))
            self._append_event_sync(db, team_id, "artifact.published", {
                "artifactId": artifact_id, "taskId": task_id, "agentId": agent_id, "kind": kind,
            })
            self._append_event_sync(db, team_id, "task.completed", {
                "taskId": task_id, "agentId": agent_id, "artifactId": artifact_id,
            })
            remaining = db.execute(
                "SELECT COUNT(*) count FROM team_tasks WHERE team_id=? AND status NOT IN ('completed','cancelled')",
                (team_id,),
            ).fetchone()["count"]
            if remaining == 0:
                db.execute("UPDATE teams SET status='completed', updated_at=? WHERE id=?", (timestamp, team_id))
                self._append_event_sync(db, team_id, "team.completed", {"artifactId": artifact_id})
            else:
                db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))
            db.commit()
        return artifact_id

    async def fail_task(self, team_id: str, task_id: str, agent_id: str, error: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._fail_task_sync, team_id, task_id, agent_id, error)

    def _fail_task_sync(self, team_id: str, task_id: str, agent_id: str, error: str) -> None:
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT attempt, max_attempts FROM team_tasks WHERE id=?", (task_id,)).fetchone()
            next_status = "pending" if task and task["attempt"] < task["max_attempts"] else "failed"
            db.execute(
                "UPDATE team_tasks SET status=?, lease_until=NULL, error=?, updated_at=? WHERE id=?",
                (next_status, error[:4000], timestamp, task_id),
            )
            db.execute("UPDATE team_agents SET status='idle', updated_at=? WHERE id=?", (timestamp, agent_id))
            self._append_event_sync(db, team_id, "run.failed", {
                "taskId": task_id, "agentId": agent_id, "error": error[:1000], "willRetry": next_status == "pending",
            })
            db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))
            db.commit()

    async def running_team_ids(self) -> list[str]:
        return await asyncio.to_thread(self._running_team_ids_sync)

    async def recover_expired_leases(self) -> int:
        """Return abandoned process-owned runs to the durable dispatch queue."""

        async with self._write_lock:
            return await asyncio.to_thread(self._recover_expired_leases_sync)

    def _recover_expired_leases_sync(self) -> int:
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT team_id, id, owner_agent_id FROM team_tasks WHERE status='running' AND lease_until IS NOT NULL AND lease_until < ?",
                (timestamp,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE team_tasks SET status='pending', lease_until=NULL, run_id=NULL, error='Previous run lease expired; queued for recovery', updated_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                if row["owner_agent_id"]:
                    db.execute(
                        "UPDATE team_agents SET status='idle', updated_at=? WHERE id=?",
                        (timestamp, row["owner_agent_id"]),
                    )
                self._append_event_sync(db, row["team_id"], "run.recovered", {"taskId": row["id"]})
            db.commit()
            return len(rows)

    def _running_team_ids_sync(self) -> list[str]:
        with self._connect() as db:
            return [row["id"] for row in db.execute("SELECT id FROM teams WHERE status='running'").fetchall()]
