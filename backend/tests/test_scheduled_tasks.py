"""Access Layer scheduled-task rule and transactional store regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from access_layer.scheduled_tasks.models import ScheduledTaskInput
from access_layer.scheduled_tasks.schedule import ScheduleValidationError, compute_next_run
from access_layer.scheduled_tasks.store import ScheduledTaskStore
from access_layer.scheduled_tasks.runtime import ScheduledTaskRuntime
from access_layer.sessions.store import SessionStore


UTC = timezone.utc


@pytest.mark.asyncio
async def test_scheduled_sessions_are_excluded_from_chat_catalog() -> None:
    store = SessionStore(storage=None)
    await store.create_session(session_id="interactive", title="普通会话")
    await store.create_session(
        session_id="scheduled", title="自动任务",
        source="scheduled", source_ref="run-1",
    )
    assert [item.id for item in await store.list_summaries()] == ["interactive"]
    scheduled = await store.get("scheduled")
    assert scheduled is not None
    assert scheduled.source == "scheduled"
    assert scheduled.source_ref == "run-1"


def task_input(**overrides) -> ScheduledTaskInput:
    payload = {
        "name": "每日摘要",
        "prompt": "总结项目进展",
        "scheduleKind": "daily",
        "localTime": "09:30",
        "timezone": "Asia/Shanghai",
        "agentKind": "k_agent",
        "modelId": "model-1",
        "reasoningEffort": "none",
        "mcpServerIds": [],
        "skillIds": [],
    }
    payload.update(overrides)
    return ScheduledTaskInput.model_validate(payload)


def test_daily_schedule_uses_task_timezone() -> None:
    next_run = compute_next_run(
        task_input(), datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    )
    assert next_run == datetime(2026, 8, 10, 1, 30, tzinfo=UTC)


def test_weekly_schedule_crosses_to_selected_day() -> None:
    next_run = compute_next_run(
        task_input(scheduleKind="weekly", weekdays=[3], localTime="10:00"),
        datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    )
    assert next_run == datetime(2026, 8, 12, 2, 0, tzinfo=UTC)


def test_once_rejects_past_time() -> None:
    with pytest.raises(ScheduleValidationError, match="future"):
        compute_next_run(
            task_input(
                scheduleKind="once", localDate="2026-08-09", localTime="09:00"
            ),
            datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_nonexistent_dst_time_is_rejected_for_once() -> None:
    with pytest.raises(ScheduleValidationError, match="does not exist"):
        compute_next_run(
            task_input(
                scheduleKind="once",
                localDate="2026-03-08",
                localTime="02:30",
                timezone="America/New_York",
            ),
            datetime(2026, 3, 1, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_claim_due_is_transactionally_unique(tmp_path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled.db")
    await store.initialize()
    now = datetime.now(UTC)
    payload = task_input(
        scheduleKind="once",
        localDate=(now + timedelta(days=1)).astimezone().date().isoformat(),
        localTime=(now + timedelta(minutes=2)).astimezone().strftime("%H:%M"),
        timezone="UTC",
    )
    task = await store.create(payload)
    # Move the deterministic test occurrence into the due set without sleeping.
    with store._connect() as db:
        db.execute(
            "UPDATE scheduled_tasks SET next_run_at=? WHERE id=?",
            ((now - timedelta(seconds=1)).isoformat(), task["id"]),
        )

    first, second = await asyncio.gather(
        store.claim_due(now=now, misfire_grace_seconds=900, lease_seconds=60),
        store.claim_due(now=now, misfire_grace_seconds=900, lease_seconds=60),
    )
    assert sorted([len(first), len(second)]) == [0, 1]
    assert len(await store.list_runs(task["id"])) == 1


@pytest.mark.asyncio
async def test_misfire_is_recorded_without_execution(tmp_path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled.db")
    await store.initialize()
    now = datetime.now(UTC)
    payload = task_input(
        scheduleKind="once",
        localDate=(now + timedelta(days=1)).date().isoformat(),
        localTime="12:00",
        timezone="UTC",
    )
    task = await store.create(payload)
    with store._connect() as db:
        db.execute(
            "UPDATE scheduled_tasks SET next_run_at=? WHERE id=?",
            ((now - timedelta(hours=1)).isoformat(), task["id"]),
        )
    assert await store.claim_due(
        now=now, misfire_grace_seconds=900, lease_seconds=60
    ) == []
    runs = await store.list_runs(task["id"])
    assert runs[0]["status"] == "missed"
    assert runs[0]["errorCode"] == "misfire_grace_exceeded"


@pytest.mark.asyncio
async def test_recovery_marks_running_without_replay(tmp_path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled.db")
    await store.initialize()
    now = datetime.now(UTC)
    payload = task_input(
        scheduleKind="once",
        localDate=(now + timedelta(days=1)).date().isoformat(),
        localTime="12:00",
        timezone="UTC",
    )
    task = await store.create(payload)
    await store.claim_manual(task["id"], 60)
    assert await store.recover_running() == 1
    runs = await store.list_runs(task["id"])
    assert runs[0]["status"] == "failed"
    assert runs[0]["errorCode"] == "worker_lost"


@pytest.mark.asyncio
async def test_runtime_reuses_access_layer_run_and_persists_success(tmp_path) -> None:
    class FakeResponse:
        async def body_iterator(self):
            yield 'data: {"type":"RUN_STARTED"}\n\n'
            yield 'data: {"type":"RUN_FINISHED"}\n\n'

    class FakeAccessLayer:
        def __init__(self) -> None:
            self.payloads = []

        async def run(self, payload):
            self.payloads.append(payload)
            response = FakeResponse()
            # Starlette exposes an async iterator property, not a callable.
            response.body_iterator = response.body_iterator()
            return response

    store = ScheduledTaskStore(tmp_path / "scheduled.db")
    access = FakeAccessLayer()
    runtime = ScheduledTaskRuntime(store=store, access_layer=access, enabled=False)
    await runtime.start()
    now = datetime.now(UTC)
    task = await store.create(task_input(
        scheduleKind="once",
        localDate=(now + timedelta(days=1)).date().isoformat(),
        localTime="12:00",
        timezone="UTC",
    ))
    claimed = await runtime.run_now(task["id"])
    assert claimed is not None
    for _ in range(50):
        if not runtime._active:
            break
        await asyncio.sleep(0.01)
    runs = await store.list_runs(task["id"])
    assert runs[0]["status"] == "succeeded"
    assert access.payloads[0].thread_id == runs[0]["sessionId"]
    assert access.payloads[0].forwarded_props["agentOptions"]["cliSessionMode"] == "ephemeral"
    assert access.payloads[0].forwarded_props["agentOptions"]["permissionMode"] == "default"
    await runtime.stop()


@pytest.mark.asyncio
async def test_full_access_permission_is_persisted_for_every_scheduled_run(tmp_path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled.db")
    await store.initialize()
    task = await store.create(task_input(permissionMode="full_access"))

    assert task["permissionMode"] == "full_access"
    reloaded = await store.get(task["id"])
    assert reloaded is not None
    assert reloaded["permissionMode"] == "full_access"
