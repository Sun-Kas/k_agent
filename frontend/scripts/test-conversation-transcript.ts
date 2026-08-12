import assert from "node:assert/strict";

import { timelineFromEvents } from "../src/components/transcript-timeline";
import type { AgUiEvent } from "../src/types";

const events: AgUiEvent[] = [
  { type: "RUN_STARTED", threadId: "scheduled-session", runId: "run-1" },
  { type: "REASONING_START", messageId: "reasoning-1" },
  { type: "REASONING_MESSAGE_START", messageId: "step-1", role: "reasoning", rawEvent: { title: "检查输入" } },
  { type: "REASONING_MESSAGE_CONTENT", messageId: "step-1", delta: "先分析。" },
  { type: "REASONING_MESSAGE_END", messageId: "step-1" },
  { type: "REASONING_END", messageId: "reasoning-1" },
  { type: "TEXT_MESSAGE_START", messageId: "text-1", role: "assistant" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "text-1", delta: "先说明。" },
  { type: "TEXT_MESSAGE_END", messageId: "text-1" },
  { type: "TOOL_CALL_START", toolCallId: "tool-1", toolCallName: "Bash", parentMessageId: "text-1" },
  { type: "TOOL_CALL_ARGS", toolCallId: "tool-1", delta: "{\"cmd\":\"pwd\"}" },
  { type: "TOOL_CALL_END", toolCallId: "tool-1" },
  { type: "TOOL_CALL_RESULT", toolCallId: "tool-1", messageId: "tool-result-1", content: "ok", role: "tool" },
  { type: "REASONING_START", messageId: "reasoning-2" },
  { type: "REASONING_MESSAGE_START", messageId: "step-2", role: "reasoning" },
  { type: "REASONING_MESSAGE_CONTENT", messageId: "step-2", delta: "继续判断。" },
  { type: "REASONING_MESSAGE_END", messageId: "step-2" },
  { type: "REASONING_END", messageId: "reasoning-2" },
  { type: "TEXT_MESSAGE_START", messageId: "text-2", role: "assistant" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "text-2", delta: "最终正文。" },
  { type: "TEXT_MESSAGE_END", messageId: "text-2" },
  { type: "RUN_FINISHED", threadId: "scheduled-session", runId: "run-1" }
];

const timeline = timelineFromEvents(events);
assert.deepEqual(timeline.map((activity) => activity.type), ["thinking", "text", "tool", "thinking", "text"]);
assert.equal(timeline[0]?.type === "thinking" && timeline[0].detail, "先分析。");
assert.equal(timeline[0]?.type === "thinking" && timeline[0].status, "complete");
assert.equal(timeline[2]?.type === "tool" && timeline[2].tool.status, "complete");
assert.equal(timeline[3]?.type === "thinking" && timeline[3].status, "complete");
assert.equal(timeline[4]?.type === "text" && timeline[4].content, "最终正文。");

const missingReasoningEnd = timelineFromEvents([
  { type: "RUN_STARTED", threadId: "scheduled-session", runId: "run-2" },
  { type: "REASONING_START", messageId: "reasoning-3" },
  { type: "REASONING_MESSAGE_CONTENT", messageId: "reasoning-3", delta: "准备调用。" },
  { type: "TOOL_CALL_START", toolCallId: "tool-2", toolCallName: "Skill" },
]);
assert.equal(
  missingReasoningEnd[0]?.type === "thinking" && missingReasoningEnd[0].status,
  "complete",
  "a tool boundary completes reasoning even when the provider omits REASONING_END",
);

const stoppedTimeline = timelineFromEvents([
  { type: "RUN_STARTED", threadId: "scheduled-session", runId: "run-stopped" },
  { type: "REASONING_START", messageId: "reasoning-stopped" },
  { type: "REASONING_MESSAGE_CONTENT", messageId: "reasoning-stopped", delta: "partial thought" },
  { type: "TOOL_CALL_START", toolCallId: "tool-stopped", toolCallName: "Bash" },
  { type: "TOOL_CALL_ARGS", toolCallId: "tool-stopped", delta: "{\"cmd\":\"sleep 10\"}" },
  {
    type: "RUN_FINISHED",
    threadId: "scheduled-session",
    runId: "run-stopped",
    result: { status: "stopped", stopped: true }
  }
]);
assert.equal(stoppedTimeline[1]?.type === "tool" && stoppedTimeline[1].tool.status, "stopped");

console.log("conversation transcript timeline tests passed");
