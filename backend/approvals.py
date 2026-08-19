"""把工具权限请求转换为 AG-UI terminal interrupt。

当前模块只管理一次 HTTP run 内的控制流，不保存跨请求状态。权限请求通过
``ApprovalInterrupt`` 终止原 Runner，随后由 ``backend.agui`` 输出标准
``RUN_FINISHED.outcome.interrupt``。持久化和 Resume 认领属于 Access Layer。
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any


def canonical_json_sha256(value: Any) -> str:
    """返回稳定 JSON 哈希，用于把用户决定绑定到准确的工具参数。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def consume_resume_authorization(
    authorization: dict[str, Any] | None,
    *,
    title: str,
    detail: dict[str, Any],
) -> dict[str, Any] | None:
    """按原请求 hash 消费一次 provider 重放授权；参数变化时返回 None。"""

    if not isinstance(authorization, dict) or authorization.get("consumed"):
        return None
    actual_hash = canonical_json_sha256({
        "target": detail.get("toolName") or title,
        "source": detail.get("source"),
        "serverId": detail.get("serverId"),
        "arguments": detail.get("arguments", detail.get("input", {})),
    })
    if actual_hash != authorization.get("requestHash"):
        return None
    decision = authorization.get("decision")
    if not isinstance(decision, dict):
        return None
    authorization["consumed"] = True
    payload = decision.get("payload")
    if decision.get("status") == "cancelled":
        return {"action": "cancel", "scope": "once"}
    if not isinstance(payload, dict) or not isinstance(payload.get("approved"), bool):
        return None
    return {
        "action": "approve" if payload["approved"] else "deny",
        "scope": payload.get("scope") if payload.get("scope") in {"once", "run"} else "once",
    }


class ApprovalInterrupt(BaseException):
    """Runner 在工具执行前抛出的非错误终止信号。

    它刻意不继承 ``Exception``：普通工具错误恢复层会捕获 Exception 并把它
    转成模型可见结果，而控制流 Interrupt 必须穿透该层到 ApprovalBroker。
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or "Agent run interrupted for approval"))
        self.payload = payload


class ApprovalBroker:
    """创建 run-scoped Interrupt，并把控制流转换为内部事件。

    该类保留原名称以降低 Runner 注入面的改动，但不再维护 Future、队列或
    ``requestId -> pending`` 内存表，因此 Backend 多 worker 不会导致 Resume
    请求命中错误进程。
    """

    def __init__(self) -> None:
        self._active_runs: dict[
            tuple[str, str], asyncio.Queue[dict[str, Any]]
        ] = {}
        self._run_closed: dict[tuple[str, str], asyncio.Event] = {}

    async def stream(
        self,
        events: AsyncIterator[dict[str, Any]],
        *,
        thread_id: str,
        run_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """透传 Runner；捕获审批中断后依次输出卡片和 terminal interrupt。"""

        key = (thread_id, run_id)
        if key in self._active_runs:
            raise RuntimeError(f"Approval stream already registered for run {run_id}")
        interrupt_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        run_closed = asyncio.Event()
        event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._active_runs[key] = interrupt_queue
        self._run_closed[key] = run_closed

        async def pump() -> None:
            try:
                async for event in events:
                    await event_queue.put(("event", event))
            except ApprovalInterrupt as interrupt:
                # 兼容直接抛出控制信号的 Runner；request() 的队列路径通常已先到达。
                if interrupt_queue.empty():
                    interrupt_queue.put_nowait(interrupt.payload)
            except BaseException as exc:
                await event_queue.put(("error", exc))
            finally:
                await event_queue.put(("done", None))

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                event_task = asyncio.create_task(event_queue.get())
                interrupt_task = asyncio.create_task(interrupt_queue.get())
                done, pending = await asyncio.wait(
                    {event_task, interrupt_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if interrupt_task in done:
                    payload = interrupt_task.result()
                    pump_task.cancel()
                    await asyncio.gather(pump_task, return_exceptions=True)
                    # Provider bridges waiting inside request() must be released
                    # before the consumer pauses on the terminal SSE frames.
                    run_closed.set()
                    yield {"type": "approval_request", "payload": payload}
                    yield {"type": "interrupt", "payload": payload}
                    return
                kind, value = event_task.result()
                if kind == "event":
                    yield value
                elif kind == "error":
                    raise value
                else:
                    return
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            self._active_runs.pop(key, None)
            run_closed.set()
            self._run_closed.pop(key, None)

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
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在已注册 run 内创建 Interrupt；本方法不会等待或执行工具。"""

        interrupt_queue = self._active_runs.get((thread_id, run_id))
        run_closed = self._run_closed.get((thread_id, run_id))
        if interrupt_queue is None or run_closed is None:
            raise RuntimeError("Approval requested outside an active run stream")
        request_detail = dict(detail or {})
        request_id = str(uuid.uuid4())
        tool_call_id = str(
            request_detail.get("callId")
            or request_detail.get("toolCallId")
            or request_id
        )
        request_hash = canonical_json_sha256({
            "target": request_detail.get("toolName") or title,
            "source": request_detail.get("source"),
            "serverId": request_detail.get("serverId"),
            "arguments": request_detail.get("arguments", request_detail.get("input", {})),
        })
        payload = {
            "id": request_id,
            "threadId": thread_id,
            "runId": run_id,
            "agentKind": agent_kind,
            "category": category,
            "title": title,
            "message": message,
            "detail": request_detail,
            "status": "pending",
            "toolCallId": tool_call_id,
            "requestHash": request_hash,
            # 下划线字段只走 Backend -> Access Layer 内部流；Gateway 落盘后剥离。
            "_checkpoint": {
                "version": 1,
                "kind": "restart_from_context",
                **dict(checkpoint or {}),
                "requestHash": request_hash,
            },
        }
        try:
            interrupt_queue.put_nowait(payload)
        except asyncio.QueueFull as exc:
            raise RuntimeError("Run already has a pending interrupt") from exc
        # 原 Runner 必须停在工具执行前，直到 stream 观察到队列并取消它；这里不
        # 返回临时决定，也不保存跨请求 Future。
        await run_closed.wait()
        return {"action": "cancel", "scope": "once"}

    async def resolve(self, *_args: Any, **_kwargs: Any) -> bool:
        """旧在线审批入口不再可用；迁移期统一返回不再 pending。"""

        return False

    async def is_pending(self, *_args: Any, **_kwargs: Any) -> bool:
        """标准 Interrupt 已结束原 Run，Backend 不再持有 pending 状态。"""

        return False

    async def cancel_run(self, **_kwargs: Any) -> None:
        """无进程内 Future 需要清理；兼容旧生命周期调用。"""

        return None
