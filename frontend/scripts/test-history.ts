import assert from "node:assert/strict";
import { mergeHistoricalMessages } from "../src/history";
import type { AgUiEvent, ChatMessage } from "../src/types";

const messages: ChatMessage[] = [
  {
    id: "user-failed",
    role: "user",
    content: "first",
    createdAt: "2026-07-29T13:07:28.000Z"
  },
  {
    id: "user-success",
    role: "user",
    content: "second",
    createdAt: "2026-07-29T13:14:31.000Z"
  },
  {
    id: "assistant-success",
    role: "assistant",
    content: "done",
    createdAt: "2026-07-29T13:14:39.000Z",
    meta: { runId: "run-success" }
  }
];
const events: AgUiEvent[] = [
  { type: "RUN_STARTED", threadId: "thread", runId: "run-failed" },
  { type: "TOOL_CALL_START", toolCallId: "tool", toolCallName: "broken-tool" },
  { type: "TOOL_CALL_END", toolCallId: "tool" },
  { type: "RUN_ERROR", message: "Unknown tool requested" },
  { type: "RUN_STARTED", threadId: "thread", runId: "run-success" },
  { type: "TEXT_MESSAGE_START", messageId: "assistant-success", role: "assistant" },
  { type: "TEXT_MESSAGE_END", messageId: "assistant-success" },
  { type: "RUN_FINISHED", threadId: "thread", runId: "run-success" }
];

const merged = mergeHistoricalMessages(messages, events, "error:");
assert.deepEqual(
  merged.map((message) => message.id),
  ["user-failed", "run-error-run-failed", "user-success", "assistant-success"]
);
assert.equal(merged[1]?.content, "error:Unknown tool requested");
assert.equal(merged[1]?.createdAt, messages[0]?.createdAt);

const persistedFailure = mergeHistoricalMessages(
  [
    messages[0],
    {
      id: "persisted-error",
      role: "assistant",
      content: "error:Unknown tool requested",
      createdAt: messages[0].createdAt,
      meta: { runId: "run-failed" }
    }
  ],
  events.slice(0, 4),
  "error:"
);
assert.equal(persistedFailure.filter((message) => message.role === "assistant").length, 1);

console.log("history replay regression tests passed");
