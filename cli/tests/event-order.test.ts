import assert from "node:assert/strict";
import test from "node:test";
import { emptyTimeline, projectEvent } from "../src/application/event-projector.js";
import type { AgUiEvent } from "../src/protocol/index.js";

test("AG-UI 活动严格保留原始顺序", () => {
  const events: AgUiEvent[] = [
    { type: "RUN_STARTED", threadId: "s1", runId: "r1" },
    { type: "REASONING_START", messageId: "thinking-1" },
    { type: "REASONING_MESSAGE_CONTENT", messageId: "step-1", delta: "先检查" },
    { type: "REASONING_END", messageId: "thinking-1" },
    { type: "TEXT_MESSAGE_START", messageId: "text-1", role: "assistant" },
    { type: "TEXT_MESSAGE_CONTENT", messageId: "text-1", delta: "阶段结论" },
    { type: "TEXT_MESSAGE_END", messageId: "text-1" },
    { type: "TOOL_CALL_START", toolCallId: "tool-1", toolCallName: "Read" },
    { type: "TOOL_CALL_ARGS", toolCallId: "tool-1", delta: "{}" },
    { type: "TOOL_CALL_END", toolCallId: "tool-1" },
    { type: "TOOL_CALL_RESULT", messageId: "result-1", toolCallId: "tool-1", content: "ok" },
    { type: "REASONING_START", messageId: "thinking-2" },
    { type: "REASONING_MESSAGE_CONTENT", messageId: "step-2", delta: "再综合" },
    { type: "REASONING_END", messageId: "thinking-2" },
    { type: "TEXT_MESSAGE_START", messageId: "text-2", role: "assistant" },
    { type: "TEXT_MESSAGE_CONTENT", messageId: "text-2", delta: "最终结果" },
    { type: "TEXT_MESSAGE_END", messageId: "text-2" },
  ];
  const state = events.reduce(projectEvent, emptyTimeline());
  assert.deepEqual(state.items.map((item) => item.kind), ["thinking", "text", "tool", "thinking", "text"]);
  assert.equal(state.items[0]?.kind === "thinking" && state.items[0].content, "先检查");
});

test("Tool 开始会封口当前 Thinking", () => {
  let state = projectEvent(emptyTimeline(), { type: "REASONING_START", messageId: "thinking" });
  state = projectEvent(state, { type: "TOOL_CALL_START", toolCallId: "tool", toolCallName: "Bash" });
  assert.equal(state.items[0]?.kind === "thinking" && state.items[0].status, "complete");
  assert.equal(state.activeReasoningId, undefined);
});

test("等待输入的 RUN_FINISHED 不把待审批 Tool 伪装成完成", () => {
  let state = projectEvent(emptyTimeline(), { type: "RUN_STARTED", threadId: "s1", runId: "r1" });
  state = projectEvent(state, { type: "TOOL_CALL_START", toolCallId: "tool", toolCallName: "Bash" });
  state = projectEvent(state, { type: "TOOL_CALL_END", toolCallId: "tool" });
  state = projectEvent(state, {
    type: "RUN_FINISHED",
    threadId: "s1",
    runId: "r1",
    outcome: { type: "interrupt", interrupts: [{ id: "i1", reason: "approval", message: "确认" }] },
  });
  assert.equal(state.runStatus, "waiting_input");
  assert.equal(state.items[0]?.kind === "tool" && state.items[0].status, "waiting");
});
