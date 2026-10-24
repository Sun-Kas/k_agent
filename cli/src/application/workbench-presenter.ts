import { emptyTimeline, projectEvent, timelineFromSession } from "./event-projector.js";
import type { AgUiEvent, SessionState } from "../protocol/index.js";

interface TeamEvent { eventId: string; seq: number; type: string; payload: Record<string, unknown>; occurredAt?: string }
const standardEvents = new Set(["RUN_STARTED", "RUN_FINISHED", "RUN_ERROR", "REASONING_START", "REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END", "REASONING_END", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT", "ACTIVITY_SNAPSHOT", "CUSTOM", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]);

/** 团队的持久事件按序投影成人可读的工作记录，保留工具、思考、审批和主管决策。 */
export function teamConversation(events: TeamEvent[]): Array<Record<string, unknown>> {
  let state = emptyTimeline();
  const rows = new Map<string, Record<string, unknown>>();
  const sync = () => { for (const item of state.items) rows.set(item.id, { ...rows.get(item.id), ...item }); };
  const approval = (p: Record<string, unknown>, id: string) => {
    const existing = rows.get(id);
    rows.set(id, { ...existing, ...p, id, kind: "approval", title: p.title ?? existing?.title ?? "需要确认", message: p.message ?? existing?.message ?? "请确认是否继续", status: p.status ?? existing?.status ?? "pending", interruptId: id, approval: p });
  };
  const metadata = new Map<string, Record<string, unknown>>();
  const notices: Record<string, string> = { "run.started": "Agent 开始执行任务", "run.failed": "运行失败", "task.submitted": "成果已提交，等待主管验收", "task.completed": "主管已验收，任务完成", "task.revision_requested": "主管要求返工", "supervisor.decision_applied": "主管决策" };
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const p = event.payload;
    if (event.type === "run.output" || event.type === "run.reasoning") {
      const previous = state.items.at(-1);
      const reasoning = event.type === "run.reasoning";
      const id = previous?.kind === (reasoning ? "thinking" : "text") ? previous.id : event.eventId;
      if (id === event.eventId) state = projectEvent(state, reasoning ? { type: "REASONING_START", messageId: id } : { type: "TEXT_MESSAGE_START", messageId: id, role: "assistant" });
      state = projectEvent(state, reasoning ? { type: "REASONING_MESSAGE_CONTENT", messageId: id, delta: String(p.delta ?? "") } : { type: "TEXT_MESSAGE_CONTENT", messageId: id, delta: String(p.delta ?? "") });
      metadata.set(id, { taskId: p.taskId, agentId: p.agentId, occurredAt: event.occurredAt });
      sync(); continue;
    }
    if (event.type === "run.activity" && p.event && typeof p.event === "object") {
      const raw = p.event as Record<string, unknown>;
      if (raw.type === "ACTIVITY_SNAPSHOT" && raw.activityType === "approval" && raw.content && typeof raw.content === "object") {
        const value = raw.content as Record<string, unknown>; approval(value, String(value.id ?? raw.messageId ?? event.eventId)); sync(); continue;
      }
      if (standardEvents.has(String(raw.type))) state = projectEvent(state, raw as unknown as AgUiEvent);
      const id = String(raw.toolCallId ?? raw.messageId ?? "");
      if (id) metadata.set(id, { taskId: p.taskId, agentId: p.agentId, occurredAt: event.occurredAt });
      sync(); continue;
    }
    if (event.type === "approval.requested" || event.type === "approval.resolved") {
      sync(); approval(p, String(p.id ?? event.eventId));
      metadata.set(String(p.id), { taskId: p.taskId, agentId: p.agentId, occurredAt: event.occurredAt });
      continue;
    }
    if (notices[event.type]) {
      const detail = p.reason ?? p.summary ?? p.error ?? "";
      state = projectEvent(state, { type: "TEXT_MESSAGE_START", role: "assistant", messageId: event.eventId });
      state = projectEvent(state, { type: "TEXT_MESSAGE_CONTENT", messageId: event.eventId, delta: `${notices[event.type]}${detail ? `：${detail}` : ""}` });
      state = projectEvent(state, { type: "TEXT_MESSAGE_END", messageId: event.eventId });
      metadata.set(event.eventId, { taskId: p.taskId, agentId: p.agentId, occurredAt: event.occurredAt });
    }
    sync();
  }
  return [...rows.values()].map((item) => ({ ...item, ...metadata.get(String(item.id)) }));
}

export function presentWorkbenchResult(operationId: string, value: unknown): unknown {
  if (["session", "auto-session"].includes(operationId) && value && typeof value === "object" && Array.isArray((value as SessionState).events)) {
    const session = value as SessionState;
    return { conversation: timelineFromSession(session).items, environment: session.environment, capabilities: session.capabilities, openInterrupts: session.openInterrupts, events: session.events, sessionId: session.sessionId };
  }
  if (operationId === "team-events" && Array.isArray(value)) return { conversation: teamConversation(value), nextSeq: value.at(-1)?.seq ?? 0, events: value };
  return value;
}
