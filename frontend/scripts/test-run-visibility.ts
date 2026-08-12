import assert from "node:assert/strict";

import { keepStoppedRunMessages, runHasVisibleActivity } from "../src/run-visibility";
import type { AgUiEvent, ChatMessage } from "../src/types";

const stoppedRunEvents: AgUiEvent[] = [
  { type: "RUN_STARTED", threadId: "session-1", runId: "run-1" },
  { type: "TEXT_MESSAGE_START", messageId: "assistant-1", role: "assistant" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "assistant-1", delta: "已经流出来的内容" }
];

assert.equal(runHasVisibleActivity(stoppedRunEvents), true);

const messages: ChatMessage[] = [
  { id: "user-1", role: "user", content: "开始测试", createdAt: "2026-08-11T00:00:00Z" },
  {
    id: "assistant-host",
    role: "assistant",
    content: "",
    createdAt: "2026-08-11T00:00:01Z",
    meta: { runId: "run-1" }
  }
];

assert.deepEqual(
  keepStoppedRunMessages(messages, "run-1", stoppedRunEvents),
  messages
);

assert.deepEqual(
  keepStoppedRunMessages(messages, "run-1", [
    { type: "RUN_STARTED", threadId: "session-1", runId: "run-1" }
  ]),
  [messages[0]]
);

console.log("stopped run visibility regression tests passed");
