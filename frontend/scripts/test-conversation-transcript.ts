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
assert.equal(timeline[2]?.type === "tool" && timeline[2].tool.status, "complete");
assert.equal(timeline[4]?.type === "text" && timeline[4].content, "最终正文。");

console.log("conversation transcript timeline tests passed");
