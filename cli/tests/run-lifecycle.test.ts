import assert from "node:assert/strict";
import test from "node:test";
import { createResumeInput } from "../src/application/run-input.js";
import { consumeRun } from "../src/application/run-lifecycle.js";
import { resolveCliConfig } from "../src/config/index.js";
import type { AccessLayerClient } from "../src/client/access-layer-client.js";
import type { AgUiRunInput, ApprovalActivity } from "../src/protocol/index.js";

test("HITL 恢复创建新 run 且不重发 messages", () => {
  const approval: ApprovalActivity = {
    id: "interrupt-1",
    threadId: "session-1",
    runId: "old-run",
    agentKind: "k_agent",
    category: "tool",
    title: "确认",
    message: "执行命令",
    detail: {},
    status: "pending",
    sequence: 1,
  };
  const input = createResumeInput(approval, resolveCliConfig({ modelId: "model-1" }), { action: "approve", scope: "once" });
  assert.equal(input.threadId, "session-1");
  assert.notEqual(input.runId, "old-run");
  assert.deepEqual(input.messages, []);
  assert.deepEqual(input.resume?.[0]?.payload, { approved: true, scope: "once" });
});

test("不确定审批再次提交时显式 reconfirm", () => {
  const approval: ApprovalActivity = {
    id: "interrupt-2", threadId: "session-2", runId: "old-run", agentKind: "k_agent",
    category: "tool", title: "确认", message: "执行命令", detail: {}, status: "unknown_outcome", sequence: 1,
  };
  const input = createResumeInput(approval, resolveCliConfig({ modelId: "model-1" }), { action: "deny" });
  assert.deepEqual(input.resume?.[0]?.payload, { approved: false, scope: "once", reconfirm: true });
});

test("SSE 在终态事件前断开时按协议错误处理", async () => {
  const client = {
    async *streamRun() {
      yield { type: "RUN_STARTED", threadId: "s1", runId: "r1" } as const;
    },
  } as unknown as AccessLayerClient;
  const input: AgUiRunInput = {
    threadId: "s1", runId: "r1", state: {}, messages: [], tools: [], context: [], forwardedProps: {},
  };
  await assert.rejects(consumeRun(client, input), /终态事件前结束/);
});
