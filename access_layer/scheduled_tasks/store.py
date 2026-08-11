"""Transactional SQLite store for scheduled Work tasks and trigger records."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from access_layer.scheduled_tasks.models import ScheduledTaskInput
from access_layer.scheduled_tasks.schedule import compute_following_run, compute_next_run


UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class ScheduledTaskConflict(RuntimeError):
    """The requested mutation conflicts with an active occurrence."""


class ScheduledTaskStore:
    """SQLite is the only truth source; the browser and runtime share this store."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    schedule_kind TEXT NOT NULL,
                    local_date TEXT,
                    weekdays_json TEXT NOT NULL,
                    local_time TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    next_run_at TEXT,
                    agent_kind TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    mcp_server_ids_json TEXT NOT NULL,
                    skill_ids_json TEXT NOT NULL,
                    permission_mode TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduled_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                    trigger_type TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_until TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT,
                    agent_run_id TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    UNIQUE(task_id, scheduled_for, trigger_type)
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(status, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_task
                    ON scheduled_runs(task_id, scheduled_for DESC);
                """
            )
            task_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
            }
            if "permission_mode" not in task_columns:
                # Existing automations retain today's behavior after migration.
                db.execute(
                    "ALTER TABLE scheduled_tasks ADD COLUMN permission_mode "
                    "TEXT NOT NULL DEFAULT 'default'"
                )

    async def create(self, payload: ScheduledTaskInput) -> dict[str, Any]:
        now = _now()
        next_run = compute_next_run(payload, now)
        task_id = uuid.uuid4().hex
        async with self._write_lock:
            await asyncio.to_thread(self._create_sync, task_id, payload, now, next_run)
        task = await self.get(task_id)
        assert task is not None
        return task

    def _create_sync(
        self, task_id: str, payload: ScheduledTaskInput, now: datetime, next_run: datetime | None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO scheduled_tasks (
                    id, name, prompt, status, schedule_kind, local_date,
                    weekdays_json, local_time, timezone, next_run_at, agent_kind,
                    model_id, reasoning_effort, mcp_server_ids_json, skill_ids_json,
                    permission_mode, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, payload.name, payload.prompt, payload.schedule_kind,
                    payload.local_date, json.dumps(payload.weekdays), payload.local_time,
                    payload.timezone, _iso(next_run) if next_run else None,
                    payload.agent_kind, payload.model_id, payload.reasoning_effort,
                    json.dumps(payload.mcp_server_ids), json.dumps(payload.skill_ids),
                    payload.permission_mode,
                    _iso(now), _iso(now),
                ),
            )

    async def list(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT t.*, r.id AS latest_id, r.trigger_type AS latest_trigger_type,
                    r.scheduled_for AS latest_scheduled_for, r.status AS latest_status,
                    r.session_id AS latest_session_id, r.agent_run_id AS latest_agent_run_id,
                    r.started_at AS latest_started_at, r.finished_at AS latest_finished_at,
                    r.error_code AS latest_error_code, r.error_message AS latest_error_message
                FROM scheduled_tasks t
                LEFT JOIN scheduled_runs r ON r.id = (
                    SELECT id FROM scheduled_runs WHERE task_id = t.id
                    ORDER BY scheduled_for DESC LIMIT 1
                )
                ORDER BY t.created_at DESC"""
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, task_id)

    def _get_sync(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT t.*, r.id AS latest_id, r.trigger_type AS latest_trigger_type,
                    r.scheduled_for AS latest_scheduled_for, r.status AS latest_status,
                    r.session_id AS latest_session_id, r.agent_run_id AS latest_agent_run_id,
                    r.started_at AS latest_started_at, r.finished_at AS latest_finished_at,
                    r.error_code AS latest_error_code, r.error_message AS latest_error_message
                FROM scheduled_tasks t
                LEFT JOIN scheduled_runs r ON r.id = (
                    SELECT id FROM scheduled_runs WHERE task_id = t.id
                    ORDER BY scheduled_for DESC LIMIT 1
                ) WHERE t.id = ?""",
                (task_id,),
            ).fetchone()
        return self._task_dict(row) if row else None

    async def update(self, task_id: str, payload: ScheduledTaskInput) -> dict[str, Any] | None:
        now = _now()
        next_run = compute_next_run(payload, now)
        async with self._write_lock:
            changed = await asyncio.to_thread(
                self._update_sync, task_id, payload, now, next_run
            )
        return await self.get(task_id) if changed else None

    def _update_sync(
        self, task_id: str, payload: ScheduledTaskInput, now: datetime, next_run: datetime | None
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE scheduled_tasks SET name=?, prompt=?, schedule_kind=?, local_date=?,
                    weekdays_json=?, local_time=?, timezone=?, next_run_at=?, agent_kind=?,
                    model_id=?, reasoning_effort=?, mcp_server_ids_json=?, skill_ids_json=?,
                    permission_mode=?, updated_at=? WHERE id=?""",
                (
                    payload.name, payload.prompt, payload.schedule_kind, payload.local_date,
                    json.dumps(payload.weekdays), payload.local_time, payload.timezone,
                    _iso(next_run) if next_run else None, payload.agent_kind, payload.model_id,
                    payload.reasoning_effort, json.dumps(payload.mcp_server_ids),
                    json.dumps(payload.skill_ids), payload.permission_mode, _iso(now), task_id,
                ),
            )
            return cursor.rowcount > 0

    async def set_status(self, task_id: str, status: str) -> dict[str, Any] | None:
        now = _now()
        async with self._write_lock:
            changed = await asyncio.to_thread(self._set_status_sync, task_id, status, now)
        return await self.get(task_id) if changed else None

    def _set_status_sync(self, task_id: str, status: str, now: datetime) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return False
            next_run_at = row["next_run_at"]
            if status == "active":
                rule = self._input_from_row(row)
                next_run = compute_next_run(rule, now)
                next_run_at = _iso(next_run) if next_run else None
            cursor = db.execute(
                "UPDATE scheduled_tasks SET status=?, next_run_at=?, updated_at=? WHERE id=?",
                (status, next_run_at, _iso(now), task_id),
            )
            return cursor.rowcount > 0

    async def delete(self, task_id: str) -> bool:
        async with self._write_lock:
            return await asyncio.to_thread(self._delete_sync, task_id)

    def _delete_sync(self, task_id: str) -> bool:
        with self._connect() as db:
            running = db.execute(
                "SELECT 1 FROM scheduled_runs WHERE task_id=? AND status='running' LIMIT 1",
                (task_id,),
            ).fetchone()
            if running:
                raise ScheduledTaskConflict("A running scheduled task cannot be deleted")
            return db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,)).rowcount > 0

    async def list_runs(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_runs_sync, task_id, limit)

    async def get_run(self, task_id: str, run_id: str) -> dict[str, Any] | None:
        """Resolve through both ids so one task cannot access another task's session."""
        return await asyncio.to_thread(self._get_run_sync, task_id, run_id)

    def _get_run_sync(self, task_id: str, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM scheduled_runs WHERE task_id=? AND id=?",
                (task_id, run_id),
            ).fetchone()
        return self._run_dict(row) if row else None

    async def list_session_refs(self) -> list[tuple[str, str]]:
        """Return old run/session links for the source-metadata backfill."""
        return await asyncio.to_thread(self._list_session_refs_sync)

    def _list_session_refs_sync(self) -> list[tuple[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, session_id FROM scheduled_runs WHERE session_id IS NOT NULL"
            ).fetchall()
        return [(str(row["session_id"]), str(row["id"])) for row in rows]

    def _list_runs_sync(self, task_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM scheduled_runs WHERE task_id=? ORDER BY scheduled_for DESC LIMIT ?",
                (task_id, max(1, min(limit, 200))),
            ).fetchall()
        return [self._run_dict(row) for row in rows]

    async def next_due_at(self) -> datetime | None:
        raw = await asyncio.to_thread(self._next_due_at_sync)
        return datetime.fromisoformat(raw) if raw else None

    def _next_due_at_sync(self) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT MIN(next_run_at) AS due FROM scheduled_tasks WHERE status='active'"
            ).fetchone()
        return row["due"] if row else None

    async def claim_due(
        self, *, now: datetime, misfire_grace_seconds: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._claim_due_sync, now, misfire_grace_seconds, lease_seconds
            )

    def _claim_due_sync(
        self, now: datetime, misfire_grace_seconds: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM scheduled_tasks
                   WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at <= ?
                   ORDER BY next_run_at LIMIT 32""",
                (_iso(now),),
            ).fetchall()
            for row in rows:
                scheduled_for = datetime.fromisoformat(row["next_run_at"])
                rule = self._input_from_row(row)
                following = compute_following_run(rule, scheduled_for)
                status = (
                    "missed"
                    if (now - scheduled_for).total_seconds() > misfire_grace_seconds
                    else "running"
                )
                run_id = uuid.uuid4().hex
                session_id = uuid.uuid4().hex if status == "running" else None
                agent_run_id = uuid.uuid4().hex if status == "running" else None
                db.execute(
                    """INSERT OR IGNORE INTO scheduled_runs (
                        id, task_id, trigger_type, scheduled_for, status, lease_until,
                        attempt, session_id, agent_run_id, started_at, finished_at,
                        error_code, error_message
                    ) VALUES (?, ?, 'schedule', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, row["id"], _iso(scheduled_for), status,
                        _iso(now + timedelta(seconds=lease_seconds)) if status == "running" else None,
                        session_id, agent_run_id, _iso(now) if status == "running" else None,
                        _iso(now) if status == "missed" else None,
                        "misfire_grace_exceeded" if status == "missed" else None,
                        "任务超过补跑宽限时间" if status == "missed" else None,
                    ),
                )
                db.execute(
                    "UPDATE scheduled_tasks SET next_run_at=?, updated_at=? WHERE id=?",
                    (_iso(following) if following else None, _iso(now), row["id"]),
                )
                if status == "running":
                    claimed.append({
                        "task": self._task_dict(row),
                        "run": {
                            "id": run_id, "taskId": row["id"], "sessionId": session_id,
                            "agentRunId": agent_run_id, "scheduledFor": _iso(scheduled_for),
                        },
                    })
        return claimed

    async def claim_manual(self, task_id: str, lease_seconds: int) -> dict[str, Any] | None:
        async with self._write_lock:
            return await asyncio.to_thread(self._claim_manual_sync, task_id, lease_seconds)

    def _claim_manual_sync(self, task_id: str, lease_seconds: int) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if db.execute(
                "SELECT 1 FROM scheduled_runs WHERE task_id=? AND status='running'", (task_id,)
            ).fetchone():
                raise ScheduledTaskConflict("This scheduled task is already running")
            run_id, session_id, agent_run_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
            db.execute(
                """INSERT INTO scheduled_runs (
                    id, task_id, trigger_type, scheduled_for, status, lease_until,
                    attempt, session_id, agent_run_id, started_at
                ) VALUES (?, ?, 'manual', ?, 'running', ?, 1, ?, ?, ?)""",
                (run_id, task_id, _iso(now), _iso(now + timedelta(seconds=lease_seconds)),
                 session_id, agent_run_id, _iso(now)),
            )
        return {"task": self._task_dict(row), "run": {
            "id": run_id, "taskId": task_id, "sessionId": session_id,
            "agentRunId": agent_run_id, "scheduledFor": _iso(now),
        }}

    async def finish_run(
        self, run_id: str, *, success: bool, error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._finish_run_sync, run_id, success, error_code, error_message
            )

    def _finish_run_sync(
        self, run_id: str, success: bool, error_code: str | None, error_message: str | None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE scheduled_runs SET status=?, lease_until=NULL, finished_at=?,
                    error_code=?, error_message=? WHERE id=?""",
                ("succeeded" if success else "failed", _iso(_now()), error_code,
                 (error_message or "")[:4000] or None, run_id),
            )

    async def recover_running(self) -> int:
        async with self._write_lock:
            return await asyncio.to_thread(self._recover_running_sync)

    def _recover_running_sync(self) -> int:
        with self._connect() as db:
            return db.execute(
                """UPDATE scheduled_runs SET status='failed', lease_until=NULL,
                    finished_at=?, error_code='worker_lost',
                    error_message='Access Layer 重启，未重放可能含副作用的运行'
                    WHERE status='running'""",
                (_iso(_now()),),
            ).rowcount

    @staticmethod
    def _input_from_row(row: sqlite3.Row) -> ScheduledTaskInput:
        return ScheduledTaskInput.model_validate({
            "name": row["name"], "prompt": row["prompt"],
            "scheduleKind": row["schedule_kind"], "localDate": row["local_date"],
            "weekdays": json.loads(row["weekdays_json"]), "localTime": row["local_time"],
            "timezone": row["timezone"], "agentKind": row["agent_kind"],
            "modelId": row["model_id"], "reasoningEffort": row["reasoning_effort"],
            "mcpServerIds": json.loads(row["mcp_server_ids_json"]),
            "skillIds": json.loads(row["skill_ids_json"]),
            "permissionMode": row["permission_mode"],
        })

    @classmethod
    def _task_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        latest = None
        if "latest_id" in row.keys() and row["latest_id"]:
            latest = {
                "id": row["latest_id"], "taskId": row["id"],
                "triggerType": row["latest_trigger_type"],
                "scheduledFor": row["latest_scheduled_for"], "status": row["latest_status"],
                "sessionId": row["latest_session_id"], "agentRunId": row["latest_agent_run_id"],
                "startedAt": row["latest_started_at"], "finishedAt": row["latest_finished_at"],
                "errorCode": row["latest_error_code"], "errorMessage": row["latest_error_message"],
            }
        return {
            "id": row["id"], "name": row["name"], "prompt": row["prompt"],
            "status": row["status"], "scheduleKind": row["schedule_kind"],
            "localDate": row["local_date"], "weekdays": json.loads(row["weekdays_json"]),
            "localTime": row["local_time"], "timezone": row["timezone"],
            "nextRunAt": row["next_run_at"], "agentKind": row["agent_kind"],
            "modelId": row["model_id"], "reasoningEffort": row["reasoning_effort"],
            "mcpServerIds": json.loads(row["mcp_server_ids_json"]),
            "skillIds": json.loads(row["skill_ids_json"]),
            "permissionMode": row["permission_mode"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "latestRun": latest,
        }

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "taskId": row["task_id"], "triggerType": row["trigger_type"],
            "scheduledFor": row["scheduled_for"], "status": row["status"],
            "sessionId": row["session_id"], "agentRunId": row["agent_run_id"],
            "startedAt": row["started_at"], "finishedAt": row["finished_at"],
            "errorCode": row["error_code"], "errorMessage": row["error_message"],
        }
