"""Run-scoped human approval coordination for every Agent runner."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _PendingApproval:
    """Internal waiter plus immutable routing metadata."""

    request_id: str
    thread_id: str
    run_id: str
    future: asyncio.Future[dict[str, Any]]


class ApprovalBroker:
    """Multiplex approval requests into a live run and resume it after a decision.

    The runner that requests approval is intentionally suspended on a Future.
    A separate producer task pumps its event iterator, allowing the HTTP stream
    to deliver the approval card while the tool call remains paused.
    """

    def __init__(self, *, timeout_seconds: float = 600.0) -> None:
        self._timeout_seconds = timeout_seconds
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
        """Merge runner events and approval lifecycle events in arrival order."""

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
        """Publish one request and wait for the matching public API decision."""

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = _PendingApproval(request_id, thread_id, run_id, future)
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
                    "payload": {
                        "id": request_id,
                        "threadId": thread_id,
                        "runId": run_id,
                        "agentKind": agent_kind,
                        "category": category,
                        "title": title,
                        "message": message,
                        "detail": detail or {},
                    },
                },
            ))
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except TimeoutError as exc:
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
        """Resolve a pending request only when all routing identifiers match."""

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
                            "id": request_id,
                            "threadId": thread_id,
                            "runId": run_id,
                            "action": decision.get("action"),
                        },
                    },
                ))
            return True

    async def cancel_run(self, *, thread_id: str, run_id: str) -> None:
        """Cancel unanswered approvals when their HTTP run stream closes."""

        async with self._lock:
            for pending in tuple(self._pending.values()):
                if pending.thread_id != thread_id or pending.run_id != run_id:
                    continue
                if not pending.future.done():
                    pending.future.cancel()

