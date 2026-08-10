"""本地优先的 Agent Team 控制面：事务型 SQLite 存储。

在请求链路中的角色：Team 路由与 TeamRuntime 的唯一真相源——团队/成员/
任务/Artifact/邮箱/事件/supervisor_jobs；写路径经 `_write_lock` +
`BEGIN IMMEDIATE`，用 lease 支持进程崩溃后的回收。

服务边界：
- Access Layer 拥有该库；Agent Backend 不读写 Team 表
- 文件系统上的 task bundle / workspace Artifact 是镜像，SQLite 为准
- supervisor_jobs 是调度硬门：提交后的验收边界未完成前，不 claim 新 worker 任务
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from access_layer.teams.models import SupervisorDecision, TeamCreateInput, TeamTaskCreateInput
from backend.home import public_home_relative_path, resolve_managed_path, to_managed_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamStore:
    """用事务持久化 Team 状态，而非进程内文件锁。"""

    def __init__(self, database_path: Path) -> None:
        """绑定 SQLite 路径；异步写操作串行化在 `_write_lock`。"""
        self.database_path = database_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """创建 WAL 数据库与幂等 schema（含升级列与 supervisor 回填）。"""

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
                    workspace_dir TEXT,
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
                    network_access INTEGER,
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
                    workspace_path TEXT,
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
                CREATE TABLE IF NOT EXISTS supervisor_jobs (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    trigger_type TEXT NOT NULL,
                    trigger_payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_until TEXT,
                    run_id TEXT,
                    error TEXT,
                    decision_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_dispatch
                    ON team_tasks(team_id, status, priority, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_replay
                    ON team_events(team_id, seq);
                CREATE INDEX IF NOT EXISTS idx_mail_recipient
                    ON mailbox_messages(team_id, recipient_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_supervisor_dispatch
                    ON supervisor_jobs(team_id, status, created_at);
                """
            )
            team_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(teams)").fetchall()
            }
            if "workspace_dir" not in team_columns:
                db.execute("ALTER TABLE teams ADD COLUMN workspace_dir TEXT")
            agent_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(team_agents)").fetchall()
            }
            if "network_access" not in agent_columns:
                # NULL inherits the Agent Backend default, which lets existing
                # Teams adopt policy changes without destructive rewrites.
                db.execute("ALTER TABLE team_agents ADD COLUMN network_access INTEGER")
            artifact_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            if "workspace_path" not in artifact_columns:
                db.execute("ALTER TABLE artifacts ADD COLUMN workspace_path TEXT")
            for team in db.execute(
                "SELECT id FROM teams WHERE workspace_dir IS NULL OR TRIM(workspace_dir)=''"
            ).fetchall():
                workspace = self.database_path.parent / team["id"] / "workspace"
                db.execute(
                    "UPDATE teams SET workspace_dir=? WHERE id=?",
                    (to_managed_path(workspace), team["id"]),
                )
            # Existing databases predate supervisor_jobs. Backfill exactly one
            # boundary per running Team so old pending work cannot bypass the
            # new manager gate after an upgrade.
            legacy_teams = db.execute(
                """
                SELECT t.id FROM teams t
                WHERE t.status='running'
                  AND NOT EXISTS (SELECT 1 FROM supervisor_jobs s WHERE s.team_id=t.id)
                  AND EXISTS (
                      SELECT 1 FROM team_tasks k
                      WHERE k.team_id=t.id AND k.status IN ('pending','ready','submitted')
                  )
                """
            ).fetchall()
            for team in legacy_teams:
                submitted = db.execute(
                    "SELECT id, owner_agent_id FROM team_tasks WHERE team_id=? AND status='submitted' ORDER BY created_at",
                    (team["id"],),
                ).fetchall()
                if submitted:
                    for task in submitted:
                        artifact = db.execute(
                            "SELECT id FROM artifacts WHERE task_id=? AND status='pending_review' ORDER BY created_at DESC LIMIT 1",
                            (task["id"],),
                        ).fetchone()
                        self._enqueue_supervisor_job_sync(db, team["id"], "task.submitted", {
                            "taskId": task["id"],
                            "agentId": task["owner_agent_id"],
                            "artifactId": artifact["id"] if artifact else None,
                            "source": "supervisor_loop_migration",
                        })
                else:
                    self._enqueue_supervisor_job_sync(
                        db,
                        team["id"],
                        "team.started",
                        {"source": "supervisor_loop_migration"},
                    )

    async def create_team(self, payload: TeamCreateInput, teams_root: Path) -> dict[str, Any]:
        """在同一事务中创建成员与可选任务草稿，并入队 team.started 主管决策。"""

        async with self._write_lock:
            return await asyncio.to_thread(self._create_team_sync, payload, teams_root)

    def _create_team_sync(self, payload: TeamCreateInput, teams_root: Path) -> dict[str, Any]:
        team_id = f"team_{uuid.uuid4().hex}"
        timestamp = _now()
        if payload.workspace_dir and payload.workspace_dir.strip():
            # Relative values resolve under $K_AGENT_HOME; absolute values stay as-is.
            team_workspace = resolve_managed_path(payload.workspace_dir)
        else:
            team_workspace = (teams_root / team_id / "workspace").resolve()
        if team_workspace.exists() and not team_workspace.is_dir():
            raise ValueError("workspaceDir must point to a directory")
        # Creating the selected directory up front turns an unwritable custom
        # location into a create-Team error instead of a late publish failure.
        try:
            team_workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"workspaceDir cannot be created: {exc}") from exc
        stored_workspace = to_managed_path(team_workspace)
        marker_path = team_workspace / ".k_agent-team.json"
        if marker_path.is_file():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("workspaceDir contains an unreadable K Agent Team marker") from exc
            if marker.get("teamId") != team_id:
                raise ValueError("workspaceDir is already owned by another Team")
        else:
            try:
                marker_path.write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "teamId": team_id,
                            "teamName": payload.name,
                            "artifactDirectory": "artifacts",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                raise ValueError(f"workspaceDir is not writable: {exc}") from exc
        agent_ids = [f"agent_{uuid.uuid4().hex}" for _ in payload.agents]
        supervisor_index = next(
            (index for index, agent in enumerate(payload.agents) if agent.is_supervisor),
            0,
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO teams
                (id, name, goal, mode, status, max_parallel, supervisor_agent_id,
                 workspace_dir, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    team_id,
                    payload.name,
                    payload.goal,
                    payload.mode,
                    payload.max_parallel,
                    agent_ids[supervisor_index],
                    stored_workspace,
                    timestamp,
                    timestamp,
                ),
            )
            for index, (agent_id, agent) in enumerate(zip(agent_ids, payload.agents)):
                capability = {
                    "mcpServerIds": agent.mcp_server_ids,
                    "skillIds": agent.skill_ids,
                }
                # Agent runs remain task-isolated; this compatibility field
                # points at the shared publication workspace, not a private cwd.
                workspace = team_workspace
                reason = (
                    "用户指定为团队主管"
                    if index == supervisor_index
                    else agent.responsibility or f"负责 {agent.role} 能力范围内的任务"
                )
                db.execute(
                    """
                    INSERT INTO team_agents
                    (id, team_id, name, role, agent_kind, model_id,
                     reasoning_effort, network_access, responsibility, status,
                     is_supervisor, creation_reason, capability_json,
                     workspace_dir, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        team_id,
                        agent.name,
                        agent.role,
                        agent.agent_kind,
                        agent.model_id,
                        agent.reasoning_effort,
                        (
                            int(agent.network_access)
                            if agent.network_access is not None
                            else None
                        ),
                        agent.responsibility,
                        1 if index == supervisor_index else 0,
                        reason,
                        json.dumps(capability, ensure_ascii=False),
                        stored_workspace,
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
            elif payload.mode == "manual":
                # Manual mode used to invent one "{role}交付" seed task per worker.
                # Those drafts reused responsibility text and later stacked under
                # real supervisor create_task work, so the board looked duplicated.
                # Both modes now wait for the supervisor team.started decision to
                # publish the actual DAG.
                pass

                # Synthesis is now a supervisor control decision, not a static
                # task that could bypass per-deliverable acceptance boundaries.
            self._append_event_sync(
                db,
                team_id,
                "team.created",
                {"name": payload.name, "mode": payload.mode, "agentCount": len(agent_ids)},
            )
            # Initial task drafts are not dispatchable until the supervisor has
            # inspected the goal, team capabilities, and proposed ownership.
            self._enqueue_supervisor_job_sync(
                db,
                team_id,
                "team.started",
                {"taskIds": task_ids, "source": "team_creation"},
            )
            db.commit()
        return self._team_snapshot_sync(team_id)

    def _enqueue_supervisor_job_sync(
        self,
        db: sqlite3.Connection,
        team_id: str,
        trigger_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Queue one durable control decision in the caller's transaction."""

        job_id = f"supervisor_{uuid.uuid4().hex}"
        timestamp = _now()
        db.execute(
            """
            INSERT INTO supervisor_jobs
            (id, team_id, trigger_type, trigger_payload_json, status, attempt,
             max_attempts, lease_until, run_id, error, decision_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', 0, 3, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (job_id, team_id, trigger_type, json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
        )
        self._append_event_sync(db, team_id, "supervisor.queued", {
            "jobId": job_id,
            "triggerType": trigger_type,
            **payload,
        })
        return job_id

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
                    "workspaceDir": public_home_relative_path(row["workspace_dir"]) or row["workspace_dir"],
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
            supervisor_job = db.execute(
                "SELECT * FROM supervisor_jobs WHERE team_id=? ORDER BY created_at DESC LIMIT 1",
                (team_id,),
            ).fetchone()
            return {
                "id": team["id"], "name": team["name"], "goal": team["goal"],
                "mode": team["mode"], "status": team["status"],
                "maxParallel": team["max_parallel"],
                "supervisorAgentId": team["supervisor_agent_id"],
                "workspaceDir": public_home_relative_path(team["workspace_dir"]) or team["workspace_dir"],
                "createdAt": team["created_at"], "updatedAt": team["updated_at"],
                "lastEventSeq": last_seq,
                "supervisorState": self._supervisor_job_dict(supervisor_job) if supervisor_job else None,
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
            "networkAccess": (
                bool(row["network_access"])
                if row["network_access"] is not None
                else None
            ),
            "responsibility": row["responsibility"], "status": row["status"],
            "isSupervisor": bool(row["is_supervisor"]),
            "creationReason": row["creation_reason"],
            "capabilities": json.loads(row["capability_json"]),
            "workspaceDir": public_home_relative_path(row["workspace_dir"]) or row["workspace_dir"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "teamId": row["team_id"], "title": row["title"],
            "description": row["description"], "taskType": row["task_type"],
            # Supervisor acceptance is a Team Runtime invariant: every worker
            # Artifact is submitted before it can become a completed task.
            "reviewRequired": True,
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
            "workspacePath": public_home_relative_path(row["workspace_path"]),
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

    @staticmethod
    def _supervisor_job_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "triggerType": row["trigger_type"],
            "status": row["status"],
            "attempt": row["attempt"],
            "maxAttempts": row["max_attempts"],
            "runId": row["run_id"],
            "error": row["error"],
            "updatedAt": row["updated_at"],
        }

    async def events_after(self, team_id: str, seq: int, limit: int = 200) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._events_after_sync, team_id, seq, limit)

    def _events_after_sync(self, team_id: str, seq: int, limit: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM team_events WHERE team_id=? AND seq>? ORDER BY seq LIMIT ?",
                (team_id, seq, limit),
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    async def events_tail(self, team_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        """Return the newest durable events in ascending seq order."""

        return await asyncio.to_thread(self._events_tail_sync, team_id, limit)

    def _events_tail_sync(self, team_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM (
                    SELECT * FROM team_events WHERE team_id=? ORDER BY seq DESC LIMIT ?
                ) newest
                ORDER BY seq
                """,
                (team_id, limit),
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    async def events_for_task(self, team_id: str, task_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        """Load the full run record for one task, independent of the live tail window."""

        return await asyncio.to_thread(self._events_for_task_sync, team_id, task_id, limit)

    def _events_for_task_sync(self, team_id: str, task_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM team_events
                WHERE team_id=?
                  AND json_extract(payload_json, '$.taskId')=?
                ORDER BY seq
                LIMIT ?
                """,
                (team_id, task_id, limit),
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "teamId": row["team_id"],
            "seq": row["seq"],
            "eventId": row["event_id"],
            "type": row["type"],
            "payload": json.loads(row["payload_json"]),
            "occurredAt": row["occurred_at"],
        }

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
            if command == "resume":
                # A paused Team caused by an exhausted supervisor job needs an
                # explicit user resume to authorize another bounded attempt set.
                failed_job = db.execute(
                    "SELECT id FROM supervisor_jobs WHERE team_id=? AND status='failed' ORDER BY updated_at DESC LIMIT 1",
                    (team_id,),
                ).fetchone()
                if failed_job:
                    db.execute(
                        "UPDATE supervisor_jobs SET status='pending', attempt=0, error=NULL, updated_at=? WHERE id=?",
                        (timestamp, failed_job["id"]),
                    )
            if command == "cancel":
                db.execute(
                    "UPDATE team_tasks SET status='cancelled', updated_at=? WHERE team_id=? AND status IN ('pending','ready','claimed','submitted')",
                    (timestamp, team_id),
                )
                db.execute(
                    "UPDATE supervisor_jobs SET status='cancelled', lease_until=NULL, updated_at=? WHERE team_id=? AND status IN ('pending','running')",
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
            self._enqueue_supervisor_job_sync(db, team_id, "task.created", {
                "taskId": task_id,
                "source": "user_api",
            })
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
            if sender_id == "user" and message_type == "user_message":
                self._enqueue_supervisor_job_sync(db, team_id, "user.instruction_added", {
                    "messageId": message_id,
                    "recipientId": recipient_id,
                })
            db.commit()
        return self._team_snapshot_sync(team_id)

    async def claim_supervisor_job(
        self, team_id: str, lease_seconds: int = 120
    ) -> dict[str, Any] | None:
        """原子租约最旧的 pending 主管决策；成功则标记 supervisor 为 busy。"""

        async with self._write_lock:
            return await asyncio.to_thread(
                self._claim_supervisor_job_sync, team_id, lease_seconds
            )

    def _claim_supervisor_job_sync(
        self, team_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        timestamp = _now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            team = db.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
            if team is None or team["status"] != "running":
                return None
            job = db.execute(
                "SELECT * FROM supervisor_jobs WHERE team_id=? AND status='pending' ORDER BY created_at LIMIT 1",
                (team_id,),
            ).fetchone()
            if job is None:
                return None
            supervisor = db.execute(
                "SELECT * FROM team_agents WHERE id=? AND team_id=? AND status='idle'",
                (team["supervisor_agent_id"], team_id),
            ).fetchone()
            if supervisor is None:
                return None
            run_id = f"supervisorrun_{uuid.uuid4().hex}"
            attempt = int(job["attempt"]) + 1
            db.execute(
                "UPDATE supervisor_jobs SET status='running', attempt=?, lease_until=?, run_id=?, updated_at=? WHERE id=? AND status='pending'",
                (attempt, lease_until, run_id, timestamp, job["id"]),
            )
            db.execute(
                "UPDATE team_agents SET status='busy', updated_at=? WHERE id=?",
                (timestamp, supervisor["id"]),
            )
            self._append_event_sync(db, team_id, "supervisor.decision_started", {
                "jobId": job["id"],
                "triggerType": job["trigger_type"],
                "agentId": supervisor["id"],
                "runId": run_id,
                "attempt": attempt,
            })
            db.commit()
            current_job = db.execute(
                "SELECT * FROM supervisor_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            return {
                "team": dict(team),
                "job": self._supervisor_job_dict(current_job),
                "triggerPayload": json.loads(job["trigger_payload_json"]),
                "supervisor": self._agent_dict(supervisor),
                "runId": run_id,
            }

    async def has_supervisor_work(self, team_id: str) -> bool:
        return await asyncio.to_thread(self._has_supervisor_work_sync, team_id)

    def _has_supervisor_work_sync(self, team_id: str) -> bool:
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM supervisor_jobs WHERE team_id=? AND status IN ('pending','running') LIMIT 1",
                (team_id,),
            ).fetchone() is not None

    async def supervisor_context(self, team_id: str, job_id: str) -> dict[str, Any]:
        """Return a bounded control-plane snapshot; raw run logs stay task-local."""

        snapshot = await self.get_team(team_id)
        if snapshot is None:
            raise ValueError("Team not found")
        return {"team": snapshot, "jobId": job_id}

    async def claim_next_task(self, team_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        """原子认领一个依赖已完成且负责人空闲的任务；有 pending 主管边界时硬门拒绝。"""

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
            # A pending supervisor boundary is a hard gate: no newly ready work
            # can escape before the manager has reviewed the preceding result.
            if db.execute(
                "SELECT 1 FROM supervisor_jobs WHERE team_id=? AND status IN ('pending','running') LIMIT 1",
                (team_id,),
            ).fetchone() is not None:
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
                # The scheduler executes assignments; it never invents one.
                # Unowned work remains behind the supervisor boundary.
                owner = (
                    db.execute(
                        "SELECT * FROM team_agents WHERE id=? AND team_id=? AND status='idle'",
                        (owner_id, team_id),
                    ).fetchone()
                    if owner_id is not None
                    else None
                )
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
            task = db.execute(
                "SELECT * FROM team_tasks WHERE id=? AND team_id=?", (task_id, team_id)
            ).fetchone()
            if task is None:
                raise ValueError("Task not found")
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

    async def submit_task(self, team_id: str, task_id: str, agent_id: str, content: str) -> str:
        """持久化 worker 交付物为 pending_review Artifact，并入队 task.submitted 主管决策。"""

        async with self._write_lock:
            return await asyncio.to_thread(self._submit_task_sync, team_id, task_id, agent_id, content)

    def _submit_task_sync(self, team_id: str, task_id: str, agent_id: str, content: str) -> str:
        import hashlib
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT * FROM team_tasks WHERE id=? AND team_id=? AND owner_agent_id=? AND status='running'",
                (task_id, team_id, agent_id),
            ).fetchone()
            if task is None:
                raise ValueError("Task run is no longer active; refusing a stale submission")
            kind = "final_answer" if task["task_type"] == "synthesis" else "report"
            db.execute(
                """
                INSERT INTO artifacts
                (id, team_id, task_id, agent_id, kind, title, content, sha256,
                 version, status, workspace_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending_review', NULL, ?)
                """,
                (artifact_id, team_id, task_id, agent_id, kind, task["title"], content,
                 hashlib.sha256(content.encode("utf-8")).hexdigest(), timestamp),
            )
            db.execute(
                "UPDATE team_tasks SET status='submitted', lease_until=NULL, updated_at=? WHERE id=? AND status='running' AND owner_agent_id=?",
                (timestamp, task_id, agent_id),
            )
            db.execute("UPDATE team_agents SET status='idle', updated_at=? WHERE id=?", (timestamp, agent_id))
            self._append_event_sync(db, team_id, "artifact.submitted", {
                "artifactId": artifact_id, "taskId": task_id, "agentId": agent_id, "kind": kind,
            })
            self._append_event_sync(db, team_id, "task.submitted", {
                "taskId": task_id, "agentId": agent_id, "artifactId": artifact_id,
            })
            self._enqueue_supervisor_job_sync(db, team_id, "task.submitted", {
                "taskId": task_id,
                "agentId": agent_id,
                "artifactId": artifact_id,
            })
            db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))
            db.commit()
        return artifact_id

    async def apply_supervisor_decision(
        self, team_id: str, job_id: str, supervisor_id: str, decision: SupervisorDecision
    ) -> dict[str, Any]:
        """校验并在同一控制面事务中提交主管全部 actions（验收/建任务/收尾等）。"""

        async with self._write_lock:
            return await asyncio.to_thread(
                self._apply_supervisor_decision_sync,
                team_id,
                job_id,
                supervisor_id,
                decision,
            )

    def _apply_supervisor_decision_sync(
        self,
        team_id: str,
        job_id: str,
        supervisor_id: str,
        decision: SupervisorDecision,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT * FROM supervisor_jobs WHERE id=? AND team_id=? AND status='running'",
                (job_id, team_id),
            ).fetchone()
            team = db.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
            if job is None or team is None:
                raise ValueError("Supervisor job is no longer active")
            if team["supervisor_agent_id"] != supervisor_id:
                raise ValueError("Only the registered supervisor can apply this decision")

            trigger_payload = json.loads(job["trigger_payload_json"])
            reviewed_task_id = (
                str(trigger_payload.get("taskId"))
                if job["trigger_type"] == "task.submitted" and trigger_payload.get("taskId")
                else None
            )
            reviewed = reviewed_task_id is None
            finish_requested = False

            # Pre-allocate IDs for every create_task action. This turns
            # taskKey/dependsOn into a proper DAG contract and supports forward
            # references without exposing database-generated IDs to the model.
            created_task_ids: dict[str, str] = {}
            for action in decision.actions:
                if action.type != "create_task":
                    continue
                task_key = str(action.task_key or "").strip()
                if task_key in created_task_ids:
                    raise ValueError(f"Duplicate create_task taskKey: {task_key}")
                created_task_ids[task_key] = f"task_{uuid.uuid4().hex}"

            def require_agent(agent_id: str | None) -> sqlite3.Row:
                row = db.execute(
                    "SELECT * FROM team_agents WHERE id=? AND team_id=?",
                    (agent_id, team_id),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Agent is not registered in this Team: {agent_id}")
                return row

            def require_task(task_id: str | None) -> sqlite3.Row:
                row = db.execute(
                    "SELECT * FROM team_tasks WHERE id=? AND team_id=?",
                    (task_id, team_id),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Task does not belong to this Team: {task_id}")
                return row

            def resolve_dependency(reference: str) -> str:
                """Resolve a same-decision taskKey or an existing durable ID."""

                reference = reference.strip()
                if not reference:
                    raise ValueError("Task dependency reference cannot be empty")
                if reference in created_task_ids:
                    return created_task_ids[reference]
                return str(require_task(reference)["id"])

            created_dependencies: dict[str, list[str]] = {}
            for action in decision.actions:
                if action.type != "create_task":
                    continue
                task_key = str(action.task_key).strip()
                task_id = created_task_ids[task_key]
                dependencies = [
                    resolve_dependency(str(reference))
                    for reference in action.depends_on
                ]
                if task_id in dependencies:
                    raise ValueError(f"Task {task_key} cannot depend on itself")
                created_dependencies[task_id] = list(dict.fromkeys(dependencies))

            def visit_created(task_id: str, visiting: set[str], visited: set[str]) -> None:
                if task_id in visited:
                    return
                if task_id in visiting:
                    raise ValueError("Supervisor plan contains a dependency cycle")
                visiting.add(task_id)
                for dependency_id in created_dependencies.get(task_id, []):
                    if dependency_id in created_dependencies:
                        visit_created(dependency_id, visiting, visited)
                visiting.remove(task_id)
                visited.add(task_id)

            visited_created: set[str] = set()
            for created_task_id in created_dependencies:
                visit_created(created_task_id, set(), visited_created)

            for action in decision.actions:
                if action.type == "approve_plan":
                    self._append_event_sync(db, team_id, "supervisor.plan_approved", {
                        "jobId": job_id,
                        "reason": action.reason,
                    })
                    continue

                if action.type == "accept_submission":
                    task = require_task(action.task_id)
                    if task["status"] != "submitted":
                        raise ValueError(f"Task is not awaiting review: {task['id']}")
                    artifact = db.execute(
                        "SELECT * FROM artifacts WHERE id=COALESCE(?, id) AND task_id=? AND status='pending_review' ORDER BY created_at DESC LIMIT 1",
                        (action.artifact_id, task["id"]),
                    ).fetchone()
                    if artifact is None:
                        raise ValueError(f"Task has no pending Artifact: {task['id']}")
                    db.execute("UPDATE artifacts SET status='accepted' WHERE id=?", (artifact["id"],))
                    db.execute(
                        "UPDATE team_tasks SET status='completed', error=NULL, updated_at=? WHERE id=?",
                        (timestamp, task["id"]),
                    )
                    self._append_event_sync(db, team_id, "artifact.accepted", {
                        "artifactId": artifact["id"],
                        "taskId": task["id"],
                        "supervisorAgentId": supervisor_id,
                        "reason": action.reason,
                    })
                    self._append_event_sync(db, team_id, "task.completed", {
                        "taskId": task["id"],
                        "agentId": task["owner_agent_id"],
                        "artifactId": artifact["id"],
                        "acceptedBy": supervisor_id,
                    })
                    reviewed = reviewed or task["id"] == reviewed_task_id
                    continue

                if action.type == "request_revision":
                    task = require_task(action.task_id)
                    if task["status"] != "submitted":
                        raise ValueError(f"Task is not awaiting review: {task['id']}")
                    db.execute(
                        "UPDATE artifacts SET status='revision_required' WHERE task_id=? AND status='pending_review'",
                        (task["id"],),
                    )
                    db.execute(
                        "UPDATE team_tasks SET status='pending', error=?, run_id=NULL, updated_at=? WHERE id=?",
                        (action.reason, timestamp, task["id"]),
                    )
                    self._append_event_sync(db, team_id, "task.revision_requested", {
                        "taskId": task["id"],
                        "agentId": task["owner_agent_id"],
                        "reason": action.reason,
                    })
                    reviewed = reviewed or task["id"] == reviewed_task_id
                    continue

                if action.type == "create_task":
                    require_agent(action.assignee_agent_id)
                    task_key = str(action.task_key).strip()
                    task_id = created_task_ids[task_key]
                    dependencies = created_dependencies[task_id]
                    db.execute(
                        """
                        INSERT INTO team_tasks VALUES
                        (?, ?, ?, ?, 'work', 'pending', ?, ?, ?, 0, 3, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            task_id,
                            team_id,
                            action.title,
                            action.description,
                            action.priority,
                            action.assignee_agent_id,
                            json.dumps(dependencies),
                            timestamp,
                            timestamp,
                        ),
                    )
                    self._append_event_sync(db, team_id, "task.created", {
                        "taskId": task_id,
                        "taskKey": task_key,
                        "title": action.title,
                        "createdBy": supervisor_id,
                        "dependsOn": dependencies,
                    })
                    self._append_event_sync(db, team_id, "task.assigned", {
                        "taskId": task_id,
                        "agentId": action.assignee_agent_id,
                        "assignedBy": supervisor_id,
                        "reason": action.reason,
                    })
                    continue

                if action.type in {"assign_task", "reassign_task"}:
                    task = require_task(action.task_id)
                    require_agent(action.assignee_agent_id)
                    if task["status"] not in {"pending", "ready", "failed"}:
                        raise ValueError(f"Task cannot be assigned in state {task['status']}: {task['id']}")
                    db.execute(
                        "UPDATE team_tasks SET owner_agent_id=?, status='pending', error=NULL, updated_at=? WHERE id=?",
                        (action.assignee_agent_id, timestamp, task["id"]),
                    )
                    self._append_event_sync(db, team_id, "task.assigned", {
                        "taskId": task["id"],
                        "agentId": action.assignee_agent_id,
                        "assignedBy": supervisor_id,
                        "reason": action.reason,
                        "reassigned": action.type == "reassign_task",
                    })
                    continue

                if action.type == "request_review":
                    source = require_task(action.task_id)
                    reviewer = require_agent(action.reviewer_agent_id)
                    if source["status"] != "completed":
                        raise ValueError("Cross-review can only reference an accepted task")
                    review_id = f"task_{uuid.uuid4().hex}"
                    db.execute(
                        """
                        INSERT INTO team_tasks VALUES
                        (?, ?, ?, ?, 'review', 'pending', ?, ?, ?, 0, 3, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            review_id,
                            team_id,
                            action.title or f"复核：{source['title']}",
                            action.description or f"独立审查任务《{source['title']}》的 Artifact，指出问题并给出结论。",
                            action.priority,
                            reviewer["id"],
                            json.dumps([source["id"]]),
                            timestamp,
                            timestamp,
                        ),
                    )
                    self._append_event_sync(db, team_id, "task.created", {
                        "taskId": review_id,
                        "title": action.title or f"复核：{source['title']}",
                        "createdBy": supervisor_id,
                    })
                    self._append_event_sync(db, team_id, "task.assigned", {
                        "taskId": review_id,
                        "agentId": reviewer["id"],
                        "assignedBy": supervisor_id,
                        "reason": action.reason,
                    })
                    continue

                if action.type == "ask_human":
                    message_id = f"mail_{uuid.uuid4().hex}"
                    db.execute(
                        "INSERT INTO mailbox_messages VALUES (?, ?, ?, 'user', 'supervisor_question', ?, '[]', 'sent', ?, NULL)",
                        (message_id, team_id, supervisor_id, action.reason, timestamp),
                    )
                    self._append_event_sync(db, team_id, "supervisor.human_requested", {
                        "jobId": job_id,
                        "messageId": message_id,
                        "reason": action.reason,
                    })
                    continue

                if action.type == "finish_team":
                    finish_requested = True
                    continue

                raise ValueError(f"Unsupported supervisor action: {action.type}")

            if not reviewed:
                raise ValueError("A task.submitted decision must accept or request revision for that task")
            if job["trigger_type"] == "team.started":
                task_count = db.execute(
                    "SELECT COUNT(*) count FROM team_tasks WHERE team_id=?",
                    (team_id,),
                ).fetchone()["count"]
                if task_count == 0:
                    raise ValueError(
                        "The initial supervisor plan must create at least one task"
                    )
                unowned = db.execute(
                    "SELECT COUNT(*) count FROM team_tasks WHERE team_id=? AND status IN ('pending','ready') AND owner_agent_id IS NULL",
                    (team_id,),
                ).fetchone()["count"]
                if unowned:
                    raise ValueError("The initial supervisor decision must assign every runnable task")

            unfinished = db.execute(
                "SELECT COUNT(*) count FROM team_tasks WHERE team_id=? AND status NOT IN ('completed','cancelled')",
                (team_id,),
            ).fetchone()["count"]
            if finish_requested:
                if unfinished:
                    raise ValueError("finish_team requires every task to be completed or cancelled")
                db.execute(
                    "UPDATE teams SET status='completed', updated_at=? WHERE id=?",
                    (timestamp, team_id),
                )
                self._append_event_sync(db, team_id, "team.completed", {
                    "supervisorAgentId": supervisor_id,
                    "reason": next(
                        action.reason for action in decision.actions if action.type == "finish_team"
                    ),
                })
            else:
                db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))

            db.execute(
                "UPDATE supervisor_jobs SET status='completed', lease_until=NULL, decision_json=?, updated_at=? WHERE id=?",
                (decision.model_dump_json(by_alias=True), timestamp, job_id),
            )
            db.execute(
                "UPDATE team_agents SET status='idle', updated_at=? WHERE id=?",
                (timestamp, supervisor_id),
            )
            self._append_event_sync(db, team_id, "supervisor.decision_applied", {
                "jobId": job_id,
                "triggerType": job["trigger_type"],
                "summary": decision.summary,
                "actions": [
                    action.model_dump(by_alias=True, exclude_none=True)
                    for action in decision.actions
                ],
            })
            db.commit()
        snapshot = self._team_snapshot_sync(team_id)
        if snapshot is None:
            raise ValueError("Team disappeared after supervisor decision")
        return snapshot

    async def record_artifact_publication(
        self, team_id: str, artifact_id: str, workspace_path: Path
    ) -> None:
        """Attach the verified filesystem publication to its accepted Artifact."""

        async with self._write_lock:
            await asyncio.to_thread(
                self._record_artifact_publication_sync,
                team_id,
                artifact_id,
                workspace_path,
            )

    def _record_artifact_publication_sync(
        self, team_id: str, artifact_id: str, workspace_path: Path
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            artifact = db.execute(
                "SELECT task_id, status, workspace_path FROM artifacts WHERE id=? AND team_id=?",
                (artifact_id, team_id),
            ).fetchone()
            if artifact is None or artifact["status"] != "accepted":
                raise ValueError("Only an accepted Team Artifact can be published")
            resolved = to_managed_path(workspace_path)
            if artifact["workspace_path"] == resolved:
                return
            db.execute(
                "UPDATE artifacts SET workspace_path=? WHERE id=?",
                (resolved, artifact_id),
            )
            self._append_event_sync(db, team_id, "artifact.published", {
                "artifactId": artifact_id,
                "taskId": artifact["task_id"],
                "workspacePath": resolved,
            })
            db.commit()

    async def fail_supervisor_job(
        self, team_id: str, job_id: str, supervisor_id: str, error: str
    ) -> None:
        """Retry invalid decisions, then pause instead of silently dropping control."""

        async with self._write_lock:
            await asyncio.to_thread(
                self._fail_supervisor_job_sync, team_id, job_id, supervisor_id, error
            )

    def _fail_supervisor_job_sync(
        self, team_id: str, job_id: str, supervisor_id: str, error: str
    ) -> None:
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT attempt, max_attempts FROM supervisor_jobs WHERE id=? AND team_id=? AND status='running'",
                (job_id, team_id),
            ).fetchone()
            if job is None:
                return
            retry = int(job["attempt"]) < int(job["max_attempts"])
            next_status = "pending" if retry else "failed"
            db.execute(
                "UPDATE supervisor_jobs SET status=?, lease_until=NULL, error=?, updated_at=? WHERE id=?",
                (next_status, error[:4000], timestamp, job_id),
            )
            db.execute(
                "UPDATE team_agents SET status='idle', updated_at=? WHERE id=?",
                (timestamp, supervisor_id),
            )
            if not retry:
                db.execute(
                    "UPDATE teams SET status='paused', updated_at=? WHERE id=?",
                    (timestamp, team_id),
                )
            self._append_event_sync(db, team_id, "supervisor.decision_failed", {
                "jobId": job_id,
                "error": error[:1000],
                "willRetry": retry,
            })
            db.commit()

    async def interrupt_supervisor_job(
        self, team_id: str, job_id: str, supervisor_id: str, reason: str
    ) -> None:
        """Requeue a process-owned control run without counting a bad decision."""

        async with self._write_lock:
            await asyncio.to_thread(
                self._interrupt_supervisor_job_sync,
                team_id,
                job_id,
                supervisor_id,
                reason,
            )

    def _interrupt_supervisor_job_sync(
        self, team_id: str, job_id: str, supervisor_id: str, reason: str
    ) -> None:
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE supervisor_jobs
                SET status='pending', attempt=MAX(0, attempt - 1), lease_until=NULL,
                    run_id=NULL, error=?, updated_at=?
                WHERE id=? AND team_id=? AND status='running'
                """,
                (reason[:4000], timestamp, job_id, team_id),
            )
            db.execute(
                "UPDATE team_agents SET status='idle', updated_at=? WHERE id=? AND team_id=?",
                (timestamp, supervisor_id, team_id),
            )
            self._append_event_sync(db, team_id, "supervisor.interrupted", {
                "jobId": job_id,
                "reason": reason[:1000],
                "requeued": True,
            })
            db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))
            db.commit()

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
            if next_status == "failed":
                self._append_event_sync(db, team_id, "task.failed", {
                    "taskId": task_id,
                    "agentId": agent_id,
                    "error": error[:1000],
                })
                self._enqueue_supervisor_job_sync(db, team_id, "task.failed", {
                    "taskId": task_id,
                    "agentId": agent_id,
                })
            db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))
            db.commit()

    async def interrupt_task(
        self, team_id: str, task_id: str, agent_id: str, reason: str
    ) -> None:
        """Return a process-cancelled run to the queue without spending an attempt."""

        async with self._write_lock:
            await asyncio.to_thread(
                self._interrupt_task_sync, team_id, task_id, agent_id, reason
            )

    def _interrupt_task_sync(
        self, team_id: str, task_id: str, agent_id: str, reason: str
    ) -> None:
        timestamp = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE team_tasks
                SET status='pending', attempt=MAX(0, attempt - 1), lease_until=NULL,
                    run_id=NULL, error=?, updated_at=?
                WHERE id=? AND team_id=? AND owner_agent_id=? AND status='running'
                """,
                (reason[:4000], timestamp, task_id, team_id, agent_id),
            )
            db.execute(
                "UPDATE team_agents SET status='idle', updated_at=? WHERE id=? AND team_id=?",
                (timestamp, agent_id, team_id),
            )
            self._append_event_sync(db, team_id, "run.interrupted", {
                "taskId": task_id,
                "agentId": agent_id,
                "reason": reason[:1000],
                "requeued": True,
            })
            db.execute("UPDATE teams SET updated_at=? WHERE id=?", (timestamp, team_id))
            db.commit()

    async def running_team_ids(self) -> list[str]:
        return await asyncio.to_thread(self._running_team_ids_sync)

    async def recover_expired_leases(self) -> int:
        """把租约过期的 running 任务/主管 job 退回可调度队列（进程崩溃恢复）。"""

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
            supervisor_rows = db.execute(
                "SELECT team_id, id FROM supervisor_jobs WHERE status='running' AND lease_until IS NOT NULL AND lease_until < ?",
                (timestamp,),
            ).fetchall()
            for row in supervisor_rows:
                team = db.execute(
                    "SELECT supervisor_agent_id FROM teams WHERE id=?", (row["team_id"],)
                ).fetchone()
                db.execute(
                    "UPDATE supervisor_jobs SET status='pending', lease_until=NULL, run_id=NULL, error='Previous supervisor lease expired; queued for recovery', updated_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                if team and team["supervisor_agent_id"]:
                    db.execute(
                        "UPDATE team_agents SET status='idle', updated_at=? WHERE id=?",
                        (timestamp, team["supervisor_agent_id"]),
                    )
                self._append_event_sync(db, row["team_id"], "supervisor.recovered", {
                    "jobId": row["id"]
                })
            db.commit()
            return len(rows) + len(supervisor_rows)

    def _running_team_ids_sync(self) -> list[str]:
        with self._connect() as db:
            return [row["id"] for row in db.execute("SELECT id FROM teams WHERE status='running'").fetchall()]
