import React from "react";
import assert from "node:assert/strict";
import test from "node:test";
import { EventEmitter } from "node:events";
import { render } from "ink-testing-library";
import { StructuredEditor } from "../src/terminal-page/panels/StructuredEditor.js";
import { ResultExplorer, type ExplorerNavigation } from "../src/terminal-page/panels/ResultExplorer.js";
import { SessionPtyHub } from "../src/application/session-pty.js";
import { normalizePreferences } from "../src/application/preferences.js";
const flush = () => new Promise((resolve) => setTimeout(resolve, 35));

test("团队成员可逐字段新增、编辑和保存，不要求手写 JSON", async () => {
  let saved = "";
  const view = render(<StructuredEditor value="[]" fieldKey="agents" onSave={(value) => { saved = value; }} onClose={() => {}} />);
  try {
    view.stdin.write("a"); await flush();
    view.stdin.write("\r"); await flush();
    assert.match(view.lastFrame() ?? "", /主管|名称/);
    view.stdin.write("\r"); await flush();
    view.stdin.write("\u0015"); view.stdin.write("分析成员"); await flush();
    view.stdin.write("\r"); await flush();
    view.stdin.write("\u0013"); await flush();
    const members = JSON.parse(saved);
    assert.equal(members[0].name, "分析成员");
    assert.equal(members[0].isSupervisor, false);
    assert.deepEqual(members[0].mcpServerIds, []);
  } finally { view.unmount(); }
});

test("任务列表选择后打开关联执行会话，无需复制 ID", async () => {
  const actions: ExplorerNavigation[] = [];
  const view = render(<ResultExplorer operationId="auto-runs" data={[{ id: "run-x", taskId: "task-x", status: "failed", errorMessage: "预算耗尽" }]} context={{ taskId: "task-x" }} onNavigate={(nav) => actions.push(nav)} onRefresh={() => {}} onBack={() => {}} />);
  try {
    view.stdin.write("\r"); await flush();
    assert.deepEqual(actions, [{ operationId: "auto-session", values: { taskId: "task-x", runId: "run-x" }, execute: true }]);
  } finally { view.unmount(); }
});

test("团队详情按成员、任务、产物分层，消息动作继承成员 ID", async () => {
  const actions: ExplorerNavigation[] = [];
  const view = render(<ResultExplorer operationId="team-detail" data={{ agents: [{ id: "agent-a", name: "研究员", role: "worker" }], tasks: [], artifacts: [] }} context={{ teamId: "team-a" }} onNavigate={(nav) => actions.push(nav)} onRefresh={() => {}} onBack={() => {}} />);
  try {
    assert.match(view.lastFrame() ?? "", /成员/);
    assert.match(view.lastFrame() ?? "", /任务看板/);
    view.stdin.write("\r"); await flush();
    view.stdin.write("m"); await flush();
    assert.deepEqual(actions, [{ operationId: "team-message", values: { teamId: "team-a", recipientId: "agent-a" }, execute: false }]);
  } finally { view.unmount(); }
});

class Socket extends EventTarget {
  readyState = 1;
  sent: Record<string, unknown>[] = [];
  send(value: string) { this.sent.push(JSON.parse(value)); }
  close() { this.readyState = 3; this.dispatchEvent(new Event("close")); }
  output(data: Buffer) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "output", data: data.toString("base64") }) })); }
}
class Input extends EventEmitter { isTTY = true; isRaw = false; setRawMode(value: boolean) { this.isRaw = value; } resume() {} }
class Output extends EventEmitter { isTTY = true; columns = 90; rows = 30; chunks: Buffer[] = []; write(value: string | Buffer) { this.chunks.push(Buffer.from(value)); return true; } }

test("PTY 原样传送 VT 和二进制分块、转发 Ctrl+C、隐藏保留、重开重放与关闭", async () => {
  const sockets: Socket[] = [];
  const hub = new SessionPtyHub("http://localhost:3001", () => { const socket = new Socket(); sockets.push(socket); return socket as unknown as WebSocket; });
  const stdin = new Input(); const stdout = new Output();
  const streams = { stdin, stdout } as unknown as { stdin: NodeJS.ReadStream; stdout: NodeJS.WriteStream };
  const first = hub.attach("s", "one", streams);
  const sequence = Buffer.from("\u001b[?1049h\u001b[3;4H中文\u001b[31mred\u001b[0m");
  sockets[0]!.output(sequence.subarray(0, 18)); sockets[0]!.output(sequence.subarray(18));
  stdin.emit("data", Buffer.from([3]));
  stdin.emit("data", Buffer.from([0x1d])); await first;
  assert.equal(sockets[0]!.readyState, 1);
  assert.equal(stdin.isRaw, false);
  assert.ok(Buffer.concat(stdout.chunks).includes(sequence));
  assert.deepEqual(sockets[0]!.sent.find((item) => item.type === "input"), { type: "input", data: "Aw==" });
  const before = stdout.chunks.length;
  const reopened = hub.attach("s", "one", streams);
  assert.equal(sockets.length, 1);
  assert.ok(Buffer.concat(stdout.chunks.slice(before)).includes(sequence));
  stdin.emit("data", Buffer.from([0x1b, 0x74]));
  assert.equal(sockets.length, 2);
  stdin.emit("data", Buffer.from([0x1b, 0x70]));
  stdin.emit("data", Buffer.from([0x1b, 0x71])); await reopened;
  assert.equal(sockets[0]!.readyState, 3);
  hub.closeAll();
});

test("界面与桌宠偏好规范化并限制范围", () => {
  const value = normalizePreferences({ theme: "ocean", fontSizeDelta: 30, pet: { petId: "doro", size: 999, opacity: 0.01 }, voice: { rate: 8, pitch: 0.1, volume: 2 } });
  assert.equal(value.theme, "ocean"); assert.equal(value.fontSizeDelta, 6);
  assert.equal(value.pet.petId, "doro"); assert.equal(value.pet.size, 168); assert.equal(value.pet.opacity, 0.45);
  // 旧配置里的朗读参数仍被收进合法范围，但 CLI 不再调用朗读。
  assert.equal(value.voice.rate, 1.4); assert.equal(value.voice.pitch, 0.7); assert.equal(value.voice.volume, 1);
});

test("整合冲突文件的解决操作自动带入路径和版本 Hash", async () => {
  const actions: ExplorerNavigation[] = [];
  const view = render(<ResultExplorer operationId="conflict" data={{ path: "src/x.ts", hash: "current-hash", content: "<<<<<<< conflict" }} context={{ sessionId: "s" }} onNavigate={(nav) => actions.push(nav)} onRefresh={() => {}} onBack={() => {}} />);
  try {
    assert.match(view.lastFrame() ?? "", /<<<<<<< conflict/);
    view.stdin.write("x"); await flush();
    assert.equal(actions[0]?.operationId, "resolve");
    assert.equal(actions[0]?.values.expectedHash, "current-hash");
    assert.equal(actions[0]?.values.path, "src/x.ts");
  } finally { view.unmount(); }
});

test("团队事件投影保留思考、工具、正文和审批的到达顺序", async () => {
  const { teamConversation } = await import("../src/application/workbench-presenter.js");
  const event = (seq: number, type: string, payload: Record<string, unknown>) => ({ seq, eventId: `e${seq}`, type, payload });
  const rows = teamConversation([
    event(1, "run.reasoning", { delta: "先核对数据", taskId: "t" }),
    event(2, "run.activity", { event: { type: "TOOL_CALL_START", toolCallId: "tool", toolCallName: "read" }, taskId: "t" }),
    event(3, "run.activity", { event: { type: "TOOL_CALL_ARGS", toolCallId: "tool", delta: '{"path":"x"}' } }),
    event(4, "run.activity", { event: { type: "TOOL_CALL_RESULT", toolCallId: "tool", messageId: "result", content: "内容" } }),
    event(5, "run.output", { delta: "已完成", taskId: "t" }),
    event(6, "approval.requested", { id: "approval", message: "是否发布", taskId: "t" }),
  ]);
  assert.deepEqual(rows.map((item) => item.kind), ["thinking", "tool", "text", "approval"]);
  assert.equal(rows[1]?.result, "内容"); assert.equal(rows[3]?.interruptId, "approval");
});

test("切换会话不会把旧会话的排队输入发到新会话，新会话可以直接发送", async () => {
  const { TerminalPage } = await import("../src/terminal-page/TerminalPage.js");
  const { emptyViewModel } = await import("../src/application/view-model.js");
  const { resolveCliConfig } = await import("../src/config/index.js");
  const actions: unknown[] = [];
  const base = { ...emptyViewModel(resolveCliConfig()), loading: false, connected: true, surface: "chat" as const, activeSessionId: "old" };
  const old = { ...base, timeline: { ...base.timeline, runStatus: "running" as const } };
  const view = render(<TerminalPage model={old} onAction={(action) => actions.push(action)} />);
  try {
    view.stdin.write("旧会话后续"); view.stdin.write("\r"); await flush();
    assert.equal(actions.length, 0);
    view.rerender(<TerminalPage model={{ ...base, activeSessionId: "new" }} onAction={(action) => actions.push(action)} />);
    await flush(); assert.equal(actions.length, 0);
    view.stdin.write("新会话问题"); view.stdin.write("\r"); await flush();
    assert.deepEqual(actions, [{ type: "submit_prompt", text: "新会话问题" }]);
  } finally { view.unmount(); }
});
