import assert from "node:assert/strict";
import test from "node:test";

import { timelineFromSession, type TimelineItem } from "../src/application/event-projector.js";
import type { AgUiEvent, ChatMessage, SessionState } from "../src/protocol/index.js";

test("多轮回放严格保留 input_message 与 RUN_STARTED 的历史顺序", () => {
  const timeline = timelineFromSession(session([
    input("u1", "你是谁", "r1"),
    ...turn("r1", "t1", "我是 K Agent"),
    input("u2", "你的作者是谁", "r2"),
    ...turn("r2", "t2", "Aokai"),
  ]));
  assert.deepEqual(kinds(timeline.items), [
    "user", "thinking", "text",
    "user", "thinking", "text",
  ]);
  assert.deepEqual(contents(timeline.items), [
    "你是谁",
    "",
    "我是 K Agent",
    "你的作者是谁",
    "",
    "Aokai",
  ]);
});

test("读历史时把累计 CONTENT 整块赋值，而不是再拼 token", () => {
  const timeline = timelineFromSession(session([
    input("u1", "hi", "r1"),
    { type: "RUN_STARTED", threadId: "s1", runId: "r1" },
    { type: "TEXT_MESSAGE_START", messageId: "a1", role: "assistant" },
    { type: "TEXT_MESSAGE_CONTENT", messageId: "a1", delta: "hello world" },
    { type: "TEXT_MESSAGE_END", messageId: "a1" },
    { type: "RUN_FINISHED", threadId: "s1", runId: "r1" },
  ]));
  const text = timeline.items.find((item) => item.kind === "text");
  assert.equal(text && text.kind === "text" ? text.content : "", "hello world");
});

test("工具、错误与下一轮提问不按类型重排", () => {
  const timeline = timelineFromSession(session([
    input("u1", "first", "r1"),
    { type: "RUN_STARTED", threadId: "s1", runId: "r1" },
    { type: "TOOL_CALL_START", toolCallId: "call-1", toolCallName: "Read" },
    { type: "TOOL_CALL_END", toolCallId: "call-1" },
    { type: "RUN_ERROR", message: "failed" },
    input("u2", "second", "r2"),
  ]));
  assert.deepEqual(kinds(timeline.items), ["user", "tool", "error", "user"]);
  assert.deepEqual(contents(timeline.items), ["first", "", "failed", "second"]);
});

function input(id: string, content: string, runId: string): AgUiEvent {
  const message: ChatMessage = {
    id,
    role: "user",
    content,
    createdAt: "2026-01-01T00:00:00.000Z",
  };
  return { type: "input_message", runId, message };
}

function turn(runId: string, textId: string, text: string): AgUiEvent[] {
  return [
    { type: "RUN_STARTED", threadId: "s1", runId },
    { type: "REASONING_START", messageId: `think-${runId}` },
    { type: "REASONING_END", messageId: `think-${runId}` },
    { type: "TEXT_MESSAGE_START", messageId: textId, role: "assistant" },
    { type: "TEXT_MESSAGE_CONTENT", messageId: textId, delta: text },
    { type: "TEXT_MESSAGE_END", messageId: textId },
    { type: "RUN_FINISHED", threadId: "s1", runId },
  ];
}

function session(events: AgUiEvent[]): SessionState {
  return { sessionId: "s1", events };
}

function kinds(items: TimelineItem[]): Array<TimelineItem["kind"]> {
  return items.map((item) => item.kind);
}

function contents(items: TimelineItem[]): string[] {
  return items.map((item) => "content" in item ? item.content : "");
}
