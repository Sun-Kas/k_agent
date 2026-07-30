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
 * Session `messages` are compact model context, while `events` are the UI
 * timeline. Failed runs may therefore exist only in events. Insert their
 * synthetic error rows beside the user turn that started the run, rather than
 * appending them after later persisted assistant messages during replay.
 */
export function mergeHistoricalMessages(
  messages: ChatMessage[],
  events: AgUiEvent[],
  requestErrorPrefix: string
): ChatMessage[] {
  const runs: HistoricalRun[] = [];
  let activeRun: HistoricalRun | null = null;

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

  // Legacy or compacted sessions may not retain one user message per run.
  // Keep unmatched failures visible without duplicating persisted error rows.
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
