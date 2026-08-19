"""Event-driven scheduler for Access Layer-owned Work tasks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any

from ag_ui.core import RunAgentInput

from access_layer.gateway import AgentAccessLayer
from access_layer.scheduled_tasks.store import ScheduledTaskStore
from backend.logging_config import log_event


logger = logging.getLogger("k_agent.access.scheduled_tasks")
UTC = timezone.utc


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


class ScheduledTaskRuntime:
    """Sleep until the nearest due task and wake immediately after CRUD changes."""

    def __init__(
        self,
        *,
        store: ScheduledTaskStore,
        access_layer: AgentAccessLayer,
        session_store=None,
        enabled: bool | None = None,
    ) -> None:
        self.store = store
        self._access_layer = access_layer
        self._session_store = session_store
        self.enabled = (
            os.getenv("SCHEDULED_TASK_RUNTIME_ENABLED", "true").lower() not in {"0", "false", "no"}
            if enabled is None else enabled
        )
        self._misfire_grace_seconds = _env_int(
            "SCHEDULED_TASK_MISFIRE_GRACE_SECONDS", 900, 0, 86_400
        )
        self._lease_seconds = _env_int("SCHEDULED_TASK_LEASE_SECONDS", 3600, 60, 86_400)
        self._clock_recheck_seconds = _env_int(
            "SCHEDULED_TASK_CLOCK_RECHECK_SECONDS", 60, 10, 3600
        )
        self._semaphore = asyncio.Semaphore(
            _env_int("SCHEDULED_TASK_MAX_ACTIVE_RUNS", 2, 1, 16)
        )
        self._wake_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self.last_loop_at: str | None = None
        self.last_loop_error: str | None = None

    async def start(self) -> None:
        await self.store.initialize()
        await self.store.recover_running()
        if self._session_store is not None:
            # Older scheduled sessions predate source metadata; classify them at startup.
            for session_id, run_id in await self.store.list_session_refs():
                await self._session_store.mark_source(session_id, "scheduled", run_id)
        self._stopping = False
        if self.enabled:
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(), name="k-agent-scheduled-task-runtime"
            )

    async def stop(self) -> None:
        self._stopping = True
        self._wake_event.set()
        if self._scheduler_task is not None:
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        active = list(self._active.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.clear()

    def wake(self) -> None:
        """CRUD calls invalidate the previously computed sleep deadline."""

        self._wake_event.set()

    async def run_now(self, task_id: str) -> dict[str, Any] | None:
        claimed = await self.store.claim_manual(task_id, self._lease_seconds)
        if claimed is not None:
            self._spawn(claimed)
        return claimed

    async def resume_approval(
        self,
        task_id: str,
        run_id: str,
        *,
        interrupt_id: str,
        action: str,
        scope: str,
    ) -> dict[str, Any]:
        """用 scheduled task 原配置启动标准 Resume Run，并更新原触发记录。"""

        task = await self.store.get(task_id)
        run = await self.store.get_run(task_id, run_id)
        if task is None or run is None or not run.get("sessionId"):
            raise KeyError(run_id)
        agent_run_id = f"scheduled-resume-{os.urandom(12).hex()}"
        open_interrupts = (
            await self._session_store.list_open_interrupts(run["sessionId"])
            if self._session_store is not None
            else []
        )
        requires_reconfirm = any(
            item.get("id") == interrupt_id
            and item.get("status") in {"unknown_outcome", "resume_failed"}
            for item in open_interrupts
        )
        payload = RunAgentInput.model_validate({
            "threadId": run["sessionId"],
            "runId": agent_run_id,
            "state": {}, "messages": [], "tools": [], "context": [],
            "resume": [{
                "interruptId": interrupt_id,
                "status": "cancelled" if action == "cancel" else "resolved",
                **(
                    {}
                    if action == "cancel"
                    else {"payload": {
                        "approved": action == "approve", "scope": scope,
                        **({"reconfirm": True} if requires_reconfirm else {}),
                    }}
                ),
            }],
            "forwardedProps": {
                "modelId": task["modelId"],
                "mcpServerIds": task["mcpServerIds"],
                "skillIds": task["skillIds"],
                "reasoningEffort": task["reasoningEffort"],
                "attachments": [],
                "agentKind": task["agentKind"],
                "agentOptions": {
                    "cliSessionMode": "ephemeral",
                    "permissionMode": task["permissionMode"],
                },
            },
        })
        response = await self._access_layer.run(payload)
        run_error: str | None = None
        interrupted = False
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
            for line in text.splitlines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event.get("type") == "RUN_ERROR":
                    run_error = str(event.get("message") or "Agent run failed")
                if (
                    event.get("type") == "RUN_FINISHED"
                    and isinstance(event.get("outcome"), dict)
                    and event["outcome"].get("type") == "interrupt"
                ):
                    interrupted = True
        if run_error:
            await self.store.finish_run(
                run_id, success=False, error_code="resume_failed",
                error_message=run_error,
            )
            raise RuntimeError(run_error)
        if interrupted:
            await self.store.finish_run(
                run_id, success=False, error_code="interrupted",
                error_message="恢复运行再次等待人工审批",
            )
            return {"ok": True, "status": "interrupted", "runId": agent_run_id}
        await self.store.finish_run(run_id, success=True)
        return {"ok": True, "status": "completed", "runId": agent_run_id}

    async def health(self) -> dict[str, Any]:
        next_due = await self.store.next_due_at()
        return {
            "enabled": self.enabled,
            "schedulerRunning": self._scheduler_task is not None and not self._scheduler_task.done(),
            "activeRuns": len(self._active),
            "nextDueAt": next_due.isoformat() if next_due else None,
            "lastLoopAt": self.last_loop_at,
            "lastLoopError": self.last_loop_error,
        }

    async def _scheduler_loop(self) -> None:
        """Dynamic sleep avoids fixed polling while a bounded recheck handles clock jumps."""

        while not self._stopping:
            try:
                # Clear before reading SQLite. A CRUD wake that arrives after this
                # point remains set, so a newly earlier deadline cannot be lost.
                self._wake_event.clear()
                self.last_loop_at = datetime.now(UTC).isoformat()
                claimed = await self.store.claim_due(
                    now=datetime.now(UTC),
                    misfire_grace_seconds=self._misfire_grace_seconds,
                    lease_seconds=self._lease_seconds,
                )
                for item in claimed:
                    self._spawn(item)
                self.last_loop_error = None
                next_due = await self.store.next_due_at()
                delay = self._clock_recheck_seconds
                if next_due is not None:
                    delay = min(
                        delay,
                        max(0.0, (next_due - datetime.now(UTC)).total_seconds()),
                    )
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_loop_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Scheduled task scheduler iteration failed")
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=self._clock_recheck_seconds
                    )
                except TimeoutError:
                    pass

    def _spawn(self, claimed: dict[str, Any]) -> None:
        run_id = str(claimed["run"]["id"])
        task = asyncio.create_task(
            self._execute(claimed), name=f"scheduled-run-{run_id}"
        )
        self._active[run_id] = task
        task.add_done_callback(lambda _done, key=run_id: self._active.pop(key, None))

    async def _execute(self, claimed: dict[str, Any]) -> None:
        task = claimed["task"]
        run = claimed["run"]
        async with self._semaphore:
            log_event(
                "scheduled.run.started",
                scheduledTaskId=task["id"], scheduledRunId=run["id"],
                threadId=run["sessionId"], runId=run["agentRunId"],
            )
            try:
                if self._session_store is not None:
                    await self._session_store.create_session(
                        session_id=run["sessionId"], title=task["name"],
                        source="scheduled", source_ref=run["id"],
                    )
                payload = RunAgentInput.model_validate({
                    "threadId": run["sessionId"],
                    "runId": run["agentRunId"],
                    "state": {},
                    "messages": [{
                        "id": f"scheduled-user-{run['id']}",
                        "role": "user",
                        "content": task["prompt"],
                    }],
                    "tools": [], "context": [],
                    "forwardedProps": {
                        "modelId": task["modelId"],
                        "mcpServerIds": task["mcpServerIds"],
                        "skillIds": task["skillIds"],
                        "reasoningEffort": task["reasoningEffort"],
                        "attachments": [],
                        "agentKind": task["agentKind"],
                        # Scheduled CLI work must be isolated; silently resuming an
                        # interactive provider session would couple unrelated runs.
                        "agentOptions": {
                            "cliSessionMode": "ephemeral",
                            "permissionMode": task["permissionMode"],
                        },
                    },
                })
                response = await self._access_layer.run(payload)
                run_error: str | None = None
                interrupted = False
                async for chunk in response.body_iterator:
                    text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
                    for line in text.splitlines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event.get("type") == "RUN_ERROR":
                            run_error = str(event.get("message") or "Agent run failed")
                        if (
                            event.get("type") == "RUN_FINISHED"
                            and isinstance(event.get("outcome"), dict)
                            and event["outcome"].get("type") == "interrupt"
                        ):
                            interrupted = True
                if run_error:
                    raise RuntimeError(run_error)
                if interrupted:
                    # terminal Interrupt 已释放 HTTP/全局并发槽；本次周期不能标为
                    # succeeded，也不会阻塞下一个调度周期等待人类数小时。
                    await self.store.finish_run(
                        run["id"], success=False, error_code="interrupted",
                        error_message="运行等待人工审批，可在对应会话中恢复",
                    )
                    return
                await self.store.finish_run(run["id"], success=True)
                log_event(
                    "scheduled.run.finished",
                    scheduledTaskId=task["id"], scheduledRunId=run["id"],
                    threadId=run["sessionId"], runId=run["agentRunId"],
                )
            except asyncio.CancelledError:
                await self.store.finish_run(
                    run["id"], success=False, error_code="runtime_stopped",
                    error_message="Access Layer 已停止，运行未重放",
                )
                raise
            except Exception as exc:
                await self.store.finish_run(
                    run["id"], success=False, error_code=type(exc).__name__,
                    error_message=str(exc) or "定时任务执行失败",
                )
                log_event(
                    "scheduled.run.failed", level=logging.ERROR,
                    scheduledTaskId=task["id"], scheduledRunId=run["id"],
                    threadId=run["sessionId"], runId=run["agentRunId"],
                    errorType=type(exc).__name__,
                )
            finally:
                self.wake()
