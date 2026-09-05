import type {
  AgUiEvent,
  ApprovalActivity,
  SessionState,
} from "../protocol/index.js";

export type ActivityStatus = "active" | "waiting" | "complete" | "error" | "stopped";

export type TimelineItem =
  | { kind: "user"; id: string; runId?: string; content: string; sequence: number }
  | { kind: "text"; id: string; runId?: string; content: string; status: ActivityStatus; sequence: number }
  | { kind: "thinking"; id: string; runId?: string; title: string; content: string; status: ActivityStatus; sequence: number }
  | { kind: "tool"; id: string; runId?: string; name: string; arguments: string; result?: string; liveOutput?: string; status: ActivityStatus; sequence: number }
  | { kind: "approval"; id: string; runId?: string; approval: ApprovalActivity; sequence: number }
  | { kind: "error"; id: string; runId?: string; content: string; sequence: number };

export interface TimelineState {
  items: TimelineItem[];
  runId: string | undefined;
  runStatus: "idle" | "running" | "waiting_input" | "complete" | "error" | "stopped";
  sequence: number;
  activeReasoningId: string | undefined;
  activeThinkingStepId: string | undefined;
  activeTextId: string | undefined;
}

export function emptyTimeline(): TimelineState {
  return {
    items: [],
    runId: undefined,
    runStatus: "idle",
    sequence: 0,
    activeReasoningId: undefined,
    activeThinkingStepId: undefined,
    activeTextId: undefined,
  };
}

export function appendUserPrompt(state: TimelineState, id: string, content: string, runId?: string): TimelineState {
  return appendItem(state, { kind: "user", id, content, ...(runId ? { runId } : {}) });
}

/**
 * 原始 AG-UI 事件按到达顺序投影，不能按 Thinking、Tool、Text 类型重新分组。
 * 更新只修改原位置的活动；新生命周期才追加新项目，保证实时流和历史回放一致。
 */
export function projectEvent(
  state: TimelineState,
  event: AgUiEvent,
  options: { history?: boolean } | number = {},
): TimelineState {
  const history = typeof options === "object" && options.history === true;
  const applyDelta = (previous: string, delta: string) =>
    history ? delta : previous + delta;
  switch (event.type) {
    case "input_message":
      return appendUserPrompt(state, event.message.id, event.message.content, event.runId);
    case "RUN_STARTED":
      return { ...state, runId: event.runId, runStatus: "running" };
    case "REASONING_START": {
      const next = appendItem(state, {
        kind: "thinking",
        id: event.messageId,
        title: "思考过程",
        content: "",
        status: "active",
        ...(state.runId ? { runId: state.runId } : {}),
      });
      return { ...next, activeReasoningId: event.messageId, activeThinkingStepId: undefined };
    }
    case "REASONING_MESSAGE_START":
      return { ...state, activeThinkingStepId: event.messageId };
    case "REASONING_MESSAGE_CONTENT":
      return updateItem(state, state.activeReasoningId ?? event.messageId, (item) =>
        item.kind === "thinking" ? { ...item, content: applyDelta(item.content, event.delta) } : item,
      );
    case "REASONING_MESSAGE_END":
      return { ...state, activeThinkingStepId: undefined };
    case "REASONING_END":
      return closeThinking(state);
    case "TOOL_CALL_START": {
      // 工具开始后前一个 Thinking 块不可继续追加；即使旧事件缺少 END 也必须封口。
      const closed = closeThinking(state);
      return appendItem(closed, {
        kind: "tool",
        id: event.toolCallId,
        name: event.toolCallName,
        arguments: "",
        status: "active",
        ...(closed.runId ? { runId: closed.runId } : {}),
      });
    }
    case "TOOL_CALL_ARGS":
      return updateItem(state, event.toolCallId, (item) =>
        item.kind === "tool" ? { ...item, arguments: applyDelta(item.arguments, event.delta), status: "active" } : item,
      );
    case "TOOL_CALL_END":
      return updateItem(state, event.toolCallId, (item) =>
        item.kind === "tool" ? { ...item, status: "waiting" } : item,
      );
    case "TOOL_CALL_RESULT":
      return updateItem(state, event.toolCallId, (item) => {
        if (item.kind !== "tool") return item;
        return {
          ...item,
          result: event.content,
          status: toolResultFailed(event.content) ? "error" : "complete",
        };
      });
    case "TEXT_MESSAGE_START": {
      const next = appendItem(closeThinking(state), {
        kind: "text",
        id: event.messageId,
        content: "",
        status: "active",
        ...(state.runId ? { runId: state.runId } : {}),
      });
      return { ...next, activeTextId: event.messageId };
    }
    case "TEXT_MESSAGE_CONTENT": {
      const target = state.items.some((item) => item.id === event.messageId)
        ? state
        : appendItem(state, {
          kind: "text",
          id: event.messageId,
          content: "",
          status: "active",
          ...(state.runId ? { runId: state.runId } : {}),
        });
      const next = updateItem(target, event.messageId, (item) =>
        item.kind === "text" ? { ...item, content: applyDelta(item.content, event.delta) } : item,
      );
      return { ...next, activeTextId: event.messageId };
    }
    case "TEXT_MESSAGE_END":
      return {
        ...updateItem(state, event.messageId, (item) =>
          item.kind === "text" ? { ...item, status: "complete" } : item),
        activeTextId: state.activeTextId === event.messageId ? undefined : state.activeTextId,
      };
    case "ACTIVITY_SNAPSHOT":
      return event.activityType === "approval" && isRecord(event.content)
        ? projectApproval(state, event.content, event.messageId)
        : state;
    case "CUSTOM":
      if (event.name === "approval_request") return projectApproval(state, event.value);
      if (event.name === "approval_resolved") return projectApproval(state, event.value);
      if (event.name === "tool_output_delta") {
        const toolCallId = String(event.value.toolCallId ?? "");
        const delta = String(event.value.delta ?? "");
        return updateItem(state, toolCallId, (item) =>
          item.kind === "tool" ? { ...item, liveOutput: `${item.liveOutput ?? ""}${delta}` } : item,
        );
      }
      return state;
    case "RUN_FINISHED": {
      const stopped = isRecord(event.result) && event.result.status === "stopped";
      const interrupted = event.outcome?.type === "interrupt";
      return {
        ...closeActiveItems(state, stopped ? "stopped" : interrupted ? "waiting" : "complete"),
        runStatus: stopped ? "stopped" : interrupted ? "waiting_input" : "complete",
        activeReasoningId: undefined,
        activeTextId: undefined,
      };
    }
    case "RUN_ERROR": {
      const failed = closeActiveItems(state, "error");
      const next = appendItem(failed, {
        kind: "error",
        id: `run-error-${failed.sequence + 1}`,
        content: event.message || "Agent 运行失败",
        ...(failed.runId ? { runId: failed.runId } : {}),
      });
      return { ...next, runStatus: "error", activeReasoningId: undefined, activeTextId: undefined };
    }
    case "STATE_SNAPSHOT":
    case "MESSAGES_SNAPSHOT":
      return state;
  }
}

export function timelineFromSession(session: SessionState): TimelineState {
  let state = emptyTimeline();
  for (const event of session.events) {
    // 落盘 events 已是累计块；history 模式整段赋值，实时流仍走 projectEvent 默认拼接。
    state = projectEvent(state, event, { history: true });
  }
  // openInterrupts 是 Access Layer 的当前 checkpoint 视图；即使旧事件窗口已裁剪，
  // CLI 仍必须把待处理输入恢复出来，并继续保持 fail-closed。
  for (const interrupt of session.openInterrupts ?? []) {
    if (state.items.some((item) => item.kind === "approval" && item.id === interrupt.id)) continue;
    state = appendOpenInterrupt(state, interrupt);
  }

  return state;
}

function appendOpenInterrupt(
  state: TimelineState,
  interrupt: NonNullable<SessionState["openInterrupts"]>[number],
): TimelineState {
  return appendItem(state, {
    kind: "approval",
    id: interrupt.id,
    runId: interrupt.runId,
    approval: { ...interrupt, sequence: state.sequence + 1 },
  });
}

export function pendingApproval(state: TimelineState): ApprovalActivity | undefined {
  return [...state.items].reverse().find((item): item is Extract<TimelineItem, { kind: "approval" }> =>
    item.kind === "approval"
      && ["pending", "submitting", "unknown_outcome", "resume_failed", "error"].includes(item.approval.status),
  )?.approval;
}

function appendItem(
  state: TimelineState,
  item: NewTimelineItem,
): TimelineState {
  const sequence = state.sequence + 1;
  return { ...state, sequence, items: [...state.items, { ...item, sequence } as TimelineItem] };
}

type NewTimelineItem = TimelineItem extends infer Item
  ? Item extends TimelineItem
    ? Omit<Item, "sequence">
    : never
  : never;

function updateItem(state: TimelineState, id: string, transform: (item: TimelineItem) => TimelineItem): TimelineState {
  if (!id) return state;
  return { ...state, items: state.items.map((item) => item.id === id ? transform(item) : item) };
}

function closeThinking(state: TimelineState): TimelineState {
  if (!state.activeReasoningId) return state;
  return {
    ...updateItem(state, state.activeReasoningId, (item) =>
      item.kind === "thinking" && item.status === "active" ? { ...item, status: "complete" } : item),
    activeReasoningId: undefined,
    activeThinkingStepId: undefined,
  };
}

function closeActiveItems(state: TimelineState, status: "complete" | "error" | "stopped" | "waiting"): TimelineState {
  return {
    ...state,
    items: state.items.map((item) => {
      if (item.kind === "thinking" && item.status === "active") return { ...item, status: status === "waiting" ? "complete" : status };
      if (item.kind === "text" && item.status === "active") return { ...item, status: "complete" };
      if (item.kind === "tool" && ["active", "waiting"].includes(item.status)) return { ...item, status };
      return item;
    }),
  };
}

function projectApproval(state: TimelineState, value: Record<string, unknown>, fallbackId = ""): TimelineState {
  const id = String(value.id ?? fallbackId);
  const runId = String(value.runId ?? state.runId ?? "");
  if (!id || !runId) return state;
  const existing = state.items.find((item): item is Extract<TimelineItem, { kind: "approval" }> =>
    item.kind === "approval" && item.id === id,
  );
  const approval: ApprovalActivity = {
    id,
    threadId: String(value.threadId ?? existing?.approval.threadId ?? ""),
    runId,
    agentKind: String(value.agentKind ?? existing?.approval.agentKind ?? "k_agent"),
    category: String(value.category ?? existing?.approval.category ?? "tool"),
    title: String(value.title ?? existing?.approval.title ?? "需要你的确认"),
    message: String(value.message ?? existing?.approval.message ?? "请确认是否继续。"),
    detail: isRecord(value.detail) ? value.detail : existing?.approval.detail ?? {},
    status: approvalStatus(value, existing?.approval.status),
    sequence: existing?.approval.sequence ?? state.sequence + 1,
    ...(typeof value.error === "string" ? { error: value.error } : {}),
    ...(isRecord(value.answers) ? { answers: value.answers as NonNullable<ApprovalActivity["answers"]> } : {}),
  };
  if (existing) {
    return updateItem(state, id, (item) => item.kind === "approval" ? { ...item, approval } : item);
  }
  return appendItem(state, { kind: "approval", id, runId, approval });
}

function approvalStatus(value: Record<string, unknown>, fallback: ApprovalActivity["status"] = "pending"): ApprovalActivity["status"] {
  const status = String(value.status ?? "");
  const action = String(value.action ?? "");
  if (status === "approved" || action === "approve") return "approved";
  if (status === "answered" || action === "answer") return "answered";
  if (status === "denied" || action === "deny") return "denied";
  if (status === "cancelled" || action === "cancel") return "cancelled";
  if (status === "expired") return "expired";
  if (status === "unknown_outcome") return "unknown_outcome";
  if (status === "resume_failed") return "resume_failed";
  if (status === "error") return "error";
  return fallback;
}

function toolResultFailed(content: string): boolean {
  try {
    const payload = JSON.parse(content) as Record<string, unknown>;
    return payload.ok === false || payload.success === false || payload.isError === true;
  } catch {
    return false;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
