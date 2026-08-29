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

    # sort_keys + 紧凑分隔符：同一对象不同插入顺序仍得到同一字节串。
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    # 前缀标明算法，Access Layer Resume 时按同规则重算并比对。
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def consume_resume_authorization(
    authorization: dict[str, Any] | None,
    *,
    title: str,
    detail: dict[str, Any],
) -> dict[str, Any] | None:
    """按原请求 hash 消费一次 provider 重放授权；参数变化时返回 None。"""

    # 非 dict、或本轮已被消费：不能再用，避免一次批准驱动多次工具调用。
    if not isinstance(authorization, dict) or authorization.get("consumed"):
        return None
    # 用当前工具名/来源/参数重算 hash；必须与打断当时写入的 requestHash 一致。
    actual_hash = canonical_json_sha256({
        "target": detail.get("toolName") or title,
        "source": detail.get("source"),
        "serverId": detail.get("serverId"),
        "arguments": detail.get("arguments", detail.get("input", {})),
    })
    # 参数变了：旧批准作废，必须重新走 HITL。
    if actual_hash != authorization.get("requestHash"):
        return None
    decision = authorization.get("decision")
    if not isinstance(decision, dict):
        return None
    # 先打 consumed，再读 payload：即使后面校验失败也不允许第二次消费。
    authorization["consumed"] = True
    payload = decision.get("payload")
    # 用户取消整张卡：告诉 Runner 不要执行这次工具。
    if decision.get("status") == "cancelled":
        return {"action": "cancel", "scope": "once"}
    if not isinstance(payload, dict):
        return None
    # AskUserQuestion 类 Resume 带的是 answers，不是 approved 布尔。
    if isinstance(payload.get("answers"), dict):
        return {
            "action": "approve",
            "scope": "once",
            "answers": payload["answers"],
        }
    # 缺布尔决定：授权无效，调用方应重新打断。
    if not isinstance(payload.get("approved"), bool):
        return None
    return {
        "action": "approve" if payload["approved"] else "deny",
        # scope=run 表示本 Run 内同类目标可复用；其它值一律降成 once。
        "scope": payload.get("scope") if payload.get("scope") in {"once", "run"} else "once",
    }


class ApprovalInterrupt(BaseException):
    """Runner 在工具执行前抛出的非错误终止信号。

    它刻意不继承 ``Exception``：普通工具错误恢复层会捕获 Exception 并把它
    转成模型可见结果，而控制流 Interrupt 必须穿透该层到 ApprovalBroker。
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or "Agent run interrupted for approval"))
        # 卡片字段给 stream() 转成 approval_request / interrupt 事件。
        self.payload = payload


class ApprovalBroker:
    """创建 run-scoped Interrupt，并把控制流转换为内部事件。

    该类保留原名称以降低 Runner 注入面的改动，但不再维护 Future、队列或
    ``requestId -> pending`` 内存表，因此 Backend 多 worker 不会导致 Resume
    请求命中错误进程。
    """

    def __init__(self) -> None:
        # (thread_id, run_id) -> 容量 1 的中断队列；request() 往里塞，stream() 取走。
        self._active_runs: dict[
            tuple[str, str], asyncio.Queue[dict[str, Any]]
        ] = {}
        # 同一 key 的关闭事件：stream() 结束时 set，好让 request() 从 wait 里醒来。
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
        # 同一 run 不能套两层 stream：否则 request() 不知道往哪条队列塞。
        if key in self._active_runs:
            raise RuntimeError(f"Approval stream already registered for run {run_id}")
        # maxsize=1：一个 run 同时只允许一张 pending 卡。
        interrupt_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        # 未 set 时，工具侧的 request() 会一直等，避免工具在中断发出前继续执行。
        run_closed = asyncio.Event()
        # pump() 把 Runner 事件/错误/结束标进这条队列，主循环再和 interrupt 赛跑。
        event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        # 登记后 request() 才能按 (thread, run) 找到队列。
        self._active_runs[key] = interrupt_queue
        self._run_closed[key] = run_closed

        async def pump() -> None:
            # 后台读 Runner：主循环才能同时等「下一个事件」和「审批中断」。
            try:
                async for event in events:
                    await event_queue.put(("event", event))
            except ApprovalInterrupt as interrupt:
                # 兼容直接 throw 的 Runner；K Agent 通常已先走 request() 入队。
                if interrupt_queue.empty():
                    interrupt_queue.put_nowait(interrupt.payload)
            except BaseException as exc:
                # CancelledError 也走这里，主循环用 raise 原样抛出。
                await event_queue.put(("error", exc))
            finally:
                # 没有更多 Runner 事件；主循环收到 done 就结束透传。
                await event_queue.put(("done", None))

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                # 两个 get() 只是挂起等待，并不执行 Runner。
                # 真正读 Runner 的是 pump()；HITL 入队的是 request()。
                event_task = asyncio.create_task(event_queue.get())
                interrupt_task = asyncio.create_task(interrupt_queue.get())
                # 赛跑：哪边队列先有数据，wait 就返回；另一边仍阻塞在 get() 上。
                # FIRST_COMPLETED 每轮只处理赢家。无 HITL 时赢家一定是 event_task，
                # 下面只 yield 一个 Runner 事件再回到 while。
                done, pending = await asyncio.wait(
                    {event_task, interrupt_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # 另一路还在 get() 上阻塞，必须取消，否则会把下一轮的数据偷走。
                for task in pending:
                    task.cancel()
                # 吞掉 CancelledError，避免它冒泡中断 stream。
                await asyncio.gather(*pending, return_exceptions=True)
                if interrupt_task in done:
                    payload = interrupt_task.result()
                    # 卡片已拿到：立刻停 Runner，不要再执行当前工具。
                    pump_task.cancel()
                    await asyncio.gather(pump_task, return_exceptions=True)
                    # 先唤醒 request()，再 yield SSE；否则 bridge 会卡到客户端读完帧。
                    run_closed.set()
                    # 给 Access Layer 落盘/展示的卡片事件。
                    yield {"type": "approval_request", "payload": payload}
                    # 终止本轮 HTTP run；Resume 是另一次请求。
                    yield {"type": "interrupt", "payload": payload}
                    return
                # Python 里一次 yield x 只把 一个 x 交给外面的 async for
                kind, value = event_task.result()
                if kind == "event":
                    # 普通 Runner 事件原样交给 agui / Gateway。
                    yield value
                elif kind == "error":
                    raise value
                else:
                    # kind == "done"：Runner 正常结束，没有 HITL。
                    return
        finally:
            # 正常结束、异常、客户端断开都要拆登记，防止下一 run 撞 key。
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            self._active_runs.pop(key, None)
            # 若 request() 仍在 wait（例如消费者取消了 SSE），这里也要把它放走。
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
        # 没有外层 stream()：中断无法进入当前 HTTP 响应。
        if interrupt_queue is None or run_closed is None:
            raise RuntimeError("Approval requested outside an active run stream")
        request_detail = dict(detail or {})
        request_id = str(uuid.uuid4())
        # 前端/AG-UI 用 toolCallId 把卡片和那次 tool_call 对上。
        tool_call_id = str(
            request_detail.get("callId")
            or request_detail.get("toolCallId")
            or request_id
        )
        # 与 consume_resume_authorization 同一套字段；Resume 时按 hash 防参数被改。
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
            # 非阻塞：队列满说明本 run 已有一张卡，不能叠第二条。
            interrupt_queue.put_nowait(payload)
        except asyncio.QueueFull as exc:
            raise RuntimeError("Run already has a pending interrupt") from exc
        # 停在工具执行前，直到 stream() set 了 run_closed。
        # 不在这里存 Future，也不返回用户决定；批准发生在另一次 Resume。
        await run_closed.wait()
        # 告诉还卡在 bridge 里的调用方：这次工具调用作废，不要继续 act。
        return {"action": "cancel", "scope": "once"}
