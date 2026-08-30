import assert from "node:assert/strict";
import test from "node:test";
import { timelineFromSession, type TimelineItem } from "../src/application/event-projector.js";
import type { AgUiEvent, ChatMessage, SessionState } from "../src/protocol/index.js";

test("多轮回放把提问插回对应 RUN_STARTED，而不是堆在时间线顶部", () => {
  const timeline = timelineFromSession(session({
    messages: [
      user("u1", "你是谁"),
      assistant("a1", "我是 K Agent", "r1"),
      user("u2", "你的作者是谁"),
      assistant("a2", "Aokai", "r2"),
    ],
    events: [
      ...turn("r1", "t1", "我是 K Agent"),
      ...turn("r2", "t2", "Aokai"),
    ],
  }));
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

test("带 runId 的用户消息仍按匹配的 run 插入", () => {
  const timeline = timelineFromSession(session({
    messages: [
      user("u1", "第一问", "r1"),
      user("u2", "第二问", "r2"),
    ],
    events: [
      ...turn("r1", "t1", "答一"),
      ...turn("r2", "t2", "答二"),
    ],
  }));
  assert.equal(timeline.items[0]?.kind, "user");
  assert.equal(timeline.items[0] && timeline.items[0].kind === "user" ? timeline.items[0].content : "", "第一问");
  const secondUser = timeline.items.find((item, index) => item.kind === "user" && index > 0);
  assert.equal(secondUser && secondUser.kind === "user" ? secondUser.content : "", "第二问");
});

test("事件窗口只保留最近一轮时，更早的提问留在窗口前面", () => {
  const timeline = timelineFromSession(session({
    messages: [
      user("u1", "旧问题"),
      user("u2", "新问题"),
    ],
    events: [...turn("r2", "t2", "新回答")],
  }));
  assert.deepEqual(contents(timeline.items.filter((item) => item.kind === "user" || item.kind === "text")), [
    "旧问题",
    "新问题",
    "新回答",
  ]);
});

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

function session(input: { messages: ChatMessage[]; events: AgUiEvent[] }): SessionState {
  return {
    sessionId: "s1",
    messages: input.messages,
    trace: [],
    tasks: [],
    thinking: [],
    events: input.events,
  };
}

function user(id: string, content: string, runId?: string): ChatMessage {
  return {
    id,
    role: "user",
    content,
    createdAt: "2026-01-01T00:00:00.000Z",
    ...(runId ? { meta: { runId } } : {}),
  };
}

function assistant(id: string, content: string, runId: string): ChatMessage {
  return {
    id,
    role: "assistant",
    content,
    createdAt: "2026-01-01T00:00:00.000Z",
    meta: { runId },
  };
}

function kinds(items: TimelineItem[]): Array<TimelineItem["kind"]> {
  return items.map((item) => item.kind);
}

function contents(items: TimelineItem[]): string[] {
  return items.map((item) => "content" in item ? item.content : "");
}
