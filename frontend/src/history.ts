/**
 * 会话历史合并：messages（模型上下文）与 events（UI 时间线）对齐失败 run 的错误气泡位置。
 */
import type { AgUiEvent, ChatMessage } from "./types";

type HistoricalRun = {
  runId: string;
  error?: string;
  createdAt?: string;
};

function eventCreatedAt(event: AgUiEvent): string | undefined {
  const record = event as AgUiEvent & {
    createdAt?: unknown;
    rawEvent?: unknown;
  };
  const rawEvent = record.rawEvent && typeof record.rawEvent === "object"
    ? record.rawEvent as Record<string, unknown>
    : undefined;
  const candidate = record.createdAt ?? rawEvent?.createdAt ?? rawEvent?.created_at;
  return typeof candidate === "string" && !Number.isNaN(new Date(candidate).getTime())
    ? candidate
    : undefined;
}

/**
 * 失败 run 可能只存在于 events（messages 是紧凑上下文）。
 * 在对应 user 回合旁插入合成错误行，而不是一律 append 到末尾（否则重放会错序）。
 */
export function mergeHistoricalMessages(
  messages: ChatMessage[],
  events: AgUiEvent[],
  requestErrorPrefix: string
): ChatMessage[] {
  const runs: HistoricalRun[] = [];
  let activeRun: HistoricalRun | null = null;

  // 扫 events：按 RUN_STARTED 划分 run，记录 RUN_ERROR 与时间戳。
  for (const event of events) {
    if (event.type === "RUN_STARTED") {
      activeRun = { runId: event.runId };
      runs.push(activeRun);
      continue;
    }
    if (!activeRun) continue;

    activeRun.createdAt = eventCreatedAt(event) ?? activeRun.createdAt;
    if (event.type === "RUN_ERROR") {
      activeRun.error = event.message;
      activeRun = null;
    } else if (event.type === "RUN_FINISHED") {
      activeRun = null;
    }
  }

  const persistedRunIds = new Set(
    messages.flatMap((message) =>
      message.role === "assistant" && message.meta?.runId
        ? [message.meta.runId]
        : []
    )
  );
  const merged: ChatMessage[] = [];
  const insertedFailures = new Set<string>();
  let userTurnIndex = 0;

  // 假定 user 消息顺序与 run 顺序一一对应；失败且未持久化则插在该 user 之后。
  for (const message of messages) {
    merged.push(message);
    if (message.role !== "user") continue;

    const run = runs[userTurnIndex];
    userTurnIndex += 1;
    if (!run?.error || persistedRunIds.has(run.runId)) continue;

    merged.push({
      id: `run-error-${run.runId}`,
      role: "assistant",
      content: `${requestErrorPrefix}${run.error}`,
      createdAt: run.createdAt ?? message.createdAt,
      meta: { runId: run.runId }
    });
    insertedFailures.add(run.runId);
  }

  // 压缩/旧会话可能 user 与 run 数量不一致；剩余失败仍要可见且不重复。
  for (const run of runs) {
    if (!run.error || persistedRunIds.has(run.runId) || insertedFailures.has(run.runId)) continue;
    merged.push({
      id: `run-error-${run.runId}`,
      role: "assistant",
      content: `${requestErrorPrefix}${run.error}`,
      createdAt: run.createdAt ?? messages.at(-1)?.createdAt ?? new Date(0).toISOString(),
      meta: { runId: run.runId }
    });
  }

  return merged;
}
