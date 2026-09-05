import assert from "node:assert/strict";

import { timelineFromEvents } from "../src/components/transcript-timeline";
import type { AgUiEvent, ChatMessage } from "../src/types";

function user(id: string, content: string): ChatMessage {
  return {
    id,
    role: "user",
    content,
    createdAt: "2026-07-29T13:07:28.000Z"
  };
}

const events: AgUiEvent[] = [
  { type: "input_message", runId: "run-failed", message: user("user-failed", "first") },
  { type: "RUN_STARTED", threadId: "thread", runId: "run-failed" },
  { type: "TOOL_CALL_START", toolCallId: "tool", toolCallName: "broken-tool" },
  { type: "TOOL_CALL_END", toolCallId: "tool" },
  { type: "RUN_ERROR", message: "Unknown tool requested" },
  { type: "input_message", runId: "run-success", message: user("user-success", "second") },
  { type: "RUN_STARTED", threadId: "thread", runId: "run-success" },
  { type: "TEXT_MESSAGE_START", messageId: "assistant-success", role: "assistant" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "assistant-success", delta: "done" },
  { type: "TEXT_MESSAGE_END", messageId: "assistant-success" },
  { type: "RUN_FINISHED", threadId: "thread", runId: "run-success" }
];

const timeline = timelineFromEvents(events);
assert.deepEqual(
  timeline.map((activity) => activity.type),
  ["user", "tool", "error", "user", "text"]
);
assert.equal(timeline.find((activity) => activity.type === "error")?.content, "Unknown tool requested");
assert.equal(timeline.find((activity) => activity.type === "text")?.content, "done");

console.log("history replay regression tests passed");
