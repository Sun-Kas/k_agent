"""按 run 挂起人类审批：把 approval 事件并入 live 流，决策后再唤醒工具调用。

所有 Runner（k_agent / Codex / Claude Code）共用本经纪。`stream` 用独立 producer
泵事件，使 HTTP 流能先下发审批卡片，而请求方仍阻塞在 Future 上。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _PendingApproval:
    """待决审批：保存完整 activity 快照，resolve 时原位更新同一张卡片。"""

    request_id: str
    thread_id: str
    run_id: str
    future: asyncio.Future[dict[str, Any]]
    payload: dict[str, Any]


class ApprovalBroker:
    """把审批请求复用进正在进行的 run，并在决策后恢复被挂起的工具调用。

    请求审批的 Runner 刻意阻塞在 Future 上；独立 producer 继续泵事件，
    这样 HTTP 流能先把审批卡交给前端，工具调用本身仍保持暂停。
    """

    def __init__(self, *, timeout_seconds: float = 600.0) -> None:
        self._timeout_seconds = timeout_seconds
        # (thread_id, run_id) → 合流队列；同 run 重复注册会拒绝，防止串流。
        self._queues: dict[tuple[str, str], asyncio.Queue[tuple[str, Any]]] = {}
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def stream(
        self,
        events: AsyncIterator[dict[str, Any]],
        *,
        thread_id: str,
        run_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """按到达顺序合并 Runner 事件与审批生命周期事件；流结束取消未决审批。"""

        key = (thread_id, run_id)
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        async with self._lock:
            if key in self._queues:
                raise RuntimeError(f"Approval stream already registered for run {run_id}")
            self._queues[key] = queue

        async def produce() -> None:
            try:
                async for event in events:
                    await queue.put(("event", event))
            except BaseException as exc:
                await queue.put(("error", exc))
            else:
                await queue.put(("done", None))

        producer = asyncio.create_task(produce())
        try:
            while True:
                kind, value = await queue.get()
                if kind == "event":
                    yield value
                elif kind == "error":
                    raise value
                else:
                    break
        finally:
            if not producer.done():
                producer.cancel()
                try:
                    await producer
                except asyncio.CancelledError:
                    pass
            await self.cancel_run(thread_id=thread_id, run_id=run_id)
            async with self._lock:
                self._queues.pop(key, None)

    async def request(
        self,
        *,
        thread_id: str,
        run_id: str,
        agent_kind: str,
        category: str,
        title: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """向当前 run 流发布 `approval_request`，阻塞直到公开 API resolve 或超时。"""

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        payload = {
            "id": request_id,
            "threadId": thread_id,
            "runId": run_id,
            "agentKind": agent_kind,
            "category": category,
            "title": title,
            "message": message,
            "detail": detail or {},
            "status": "pending",
        }
        pending = _PendingApproval(request_id, thread_id, run_id, future, payload)
        key = (thread_id, run_id)
        async with self._lock:
            queue = self._queues.get(key)
            if queue is None:
                raise RuntimeError("Approval requested outside an active run stream")
            self._pending[request_id] = pending
            await queue.put((
                "event",
                {
                    "type": "approval_request",
                    "payload": payload,
                },
            ))
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except TimeoutError as exc:
            # Timeout is a lifecycle transition, not merely an exception. Emit
            # it before removing the pending entry so streamed and persisted UI
            # state cannot leave an actionable card behind forever.
            async with self._lock:
                queue = self._queues.get(key)
                if queue is not None:
                    await queue.put((
                        "event",
                        {
                            "type": "approval_resolved",
                            "payload": {
                                **pending.payload,
                                "action": "expired",
                                "status": "expired",
                            },
                        },
                    ))
            raise RuntimeError("Human approval timed out") from exc
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)

    async def resolve(
        self,
        request_id: str,
        *,
        thread_id: str,
        run_id: str,
        decision: dict[str, Any],
    ) -> bool:
        """仅当 requestId + threadId + runId 全匹配时完成 Future，防止跨 run 误唤醒。"""

        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            if pending.thread_id != thread_id or pending.run_id != run_id:
                return False
            if pending.future.done():
                return False
            pending.future.set_result(decision)
            queue = self._queues.get((thread_id, run_id))
            if queue is not None:
                await queue.put((
                    "event",
                    {
                        "type": "approval_resolved",
                        "payload": {
                            **pending.payload,
                            "action": decision.get("action"),
                            "status": (
                                "approved"
                                if decision.get("action") == "approve"
                                else "denied"
                                if decision.get("action") == "deny"
                                else "cancelled"
                            ),
                        },
                    },
                ))
            return True

    async def is_pending(
        self, request_id: str, *, thread_id: str, run_id: str
    ) -> bool:
        """Check the exact run-scoped request without exposing another run's state."""

        async with self._lock:
            pending = self._pending.get(request_id)
            return bool(
                pending is not None
                and pending.thread_id == thread_id
                and pending.run_id == run_id
                and not pending.future.done()
            )

    async def cancel_run(self, *, thread_id: str, run_id: str) -> None:
        """HTTP 流关闭时取消该 run 上仍在等待的审批，避免 Future 泄漏。"""

        async with self._lock:
            for pending in tuple(self._pending.values()):
                if pending.thread_id != thread_id or pending.run_id != run_id:
                    continue
                if not pending.future.done():
                    pending.future.cancel()
