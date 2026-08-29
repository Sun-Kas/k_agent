import React from "react";
import assert from "node:assert/strict";
import test from "node:test";
import { render } from "ink-testing-library";
import { resolveCliConfig } from "../src/config/index.js";
import { emptyViewModel } from "../src/application/view-model.js";
import { emptyTimeline, projectEvent } from "../src/application/event-projector.js";
import { TerminalPage } from "../src/terminal-page/index.js";
import type { TerminalPageAction } from "../src/terminal-page/index.js";

test("TerminalPage 在紧凑终端保持主内容和 Composer 可见", () => {
  const view = render(<TerminalPage model={chatModel()} onAction={() => {}} />);
  const frame = view.lastFrame() ?? "";
  assert.match(frame, /K Agent/);
  assert.match(frame, /CONVERSATION/);
  assert.match(frame, /❯/);
  assert.doesNotMatch(frame, /描述目标、约束或下一步/);
  assert.doesNotMatch(frame, /SESSIONS/);
  view.unmount();
});

test("Composer 只把提交意图交给 application 层", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("检查项目");
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, [{ type: "submit_prompt", text: "检查项目" }]);
  view.unmount();
});

test("输入框支持终端光标移动并在光标处插入", async () => {
  const view = render(<TerminalPage model={chatModel()} onAction={() => {}} />);
  view.stdin.write("abc");
  await flush();
  view.stdin.write("\u001b[D");
  view.stdin.write("\u001b[D");
  await flush();
  view.stdin.write("X");
  await flush();
  assert.match(stripStyles(view.lastFrame() ?? ""), /aXbc/);
  view.unmount();
});

test("中文上屏插在光标处而不是行尾", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("你作者是谁的");
  await flush();
  for (let index = 0; index < 3; index += 1) view.stdin.write("\u001b[D");
  await flush();
  view.stdin.write("四大");
  await flush();
  assert.match(stripStyles(view.lastFrame() ?? ""), /你作者四大是谁的/);
  assert.doesNotMatch(stripStyles(view.lastFrame() ?? ""), /你作者是谁的四大/);
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, [{ type: "submit_prompt", text: "你作者四大是谁的" }]);
  view.unmount();
});

test("Ctrl+U 清空当前草稿而不提交", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("待删除内容");
  await flush();
  view.stdin.write("\u0015");
  await flush();
  assert.doesNotMatch(stripStyles(view.lastFrame() ?? ""), /待删除内容/);
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, []);
  view.unmount();
});

test("按 / 展开命令选择栏并用 Enter 执行选中命令", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("/");
  await flush();
  const frame = stripStyles(view.lastFrame() ?? "");
  assert.match(frame, /\/new/);
  assert.match(frame, /Tab 补全/);
  view.stdin.write("model");
  await flush();
  assert.match(stripStyles(view.lastFrame() ?? ""), /\/model/);
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, [{ type: "slash_command", command: "model", arguments: "" }]);
  view.unmount();
});

test("Esc 收起命令选择栏且不提交任何动作", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("/");
  await flush();
  assert.match(stripStyles(view.lastFrame() ?? ""), /\/doctor/);
  view.stdin.write("\u001b");
  await flush();
  assert.doesNotMatch(stripStyles(view.lastFrame() ?? ""), /\/doctor/);
  assert.deepEqual(actions, []);
  view.unmount();
});

test("Ctrl+K 面板与 / 选择栏共用同一命令目录，可搜索并执行", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("\u000b");
  await flush();
  assert.match(stripStyles(view.lastFrame() ?? ""), /\/new/);
  view.stdin.write("doc");
  await flush();
  const frame = stripStyles(view.lastFrame() ?? "");
  assert.match(frame, /\/doctor/);
  assert.doesNotMatch(frame, /\/new/);
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, [{ type: "slash_command", command: "doctor", arguments: "" }]);
  view.unmount();
});

test("Ctrl+K 在 Run 运行中仍可用，此时 / 无法展开", async () => {
  const running = { ...chatModel(), timeline: { ...chatModel().timeline, runStatus: "running" as const } };
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={running} onAction={(action) => actions.push(action)} />);
  // 运行中输入框被禁用，`/` 进不去草稿，选择栏不会展开。
  view.stdin.write("/");
  await flush();
  assert.doesNotMatch(stripStyles(view.lastFrame() ?? ""), /Tab 补全/);
  view.stdin.write("\u000b");
  await flush();
  view.stdin.write("stop");
  await flush();
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, [{ type: "slash_command", command: "stop", arguments: "" }]);
  view.unmount();
});

test("面板拒绝执行不可用命令，但仍显示该命令与原因", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("\u000b");
  await flush();
  view.stdin.write("stop");
  await flush();
  const frame = stripStyles(view.lastFrame() ?? "");
  assert.match(frame, /\/stop/);
  assert.match(frame, /没有运行中的任务/);
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, []);
  view.unmount();
});

test("需要参数的命令只补全草稿，不提交缺参数动作", async () => {
  const model = { ...chatModel(), activeSessionId: "s1" };
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={model} onAction={(action) => actions.push(action)} />);
  view.stdin.write("\u000b");
  await flush();
  view.stdin.write("workspace");
  await flush();
  view.stdin.write("\r");
  await flush();
  assert.deepEqual(actions, []);
  assert.match(stripStyles(view.lastFrame() ?? ""), /\/workspace /);
  view.unmount();
});

test("Esc 不会批准审批，允许动作必须显式输入", async () => {
  let timeline = projectEvent(emptyTimeline(), { type: "RUN_STARTED", threadId: "s1", runId: "r1" });
  timeline = projectEvent(timeline, {
    type: "CUSTOM",
    name: "approval_request",
    value: { id: "i1", threadId: "s1", runId: "r1", title: "执行命令", message: "npm test", detail: {} },
  });
  const model = { ...chatModel(), timeline };
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={model} onAction={(action) => actions.push(action)} />);
  assert.match(view.lastFrame() ?? "", /需要你的确认/);
  view.stdin.write("\u001b");
  await flush();
  assert.deepEqual(actions, []);
  view.stdin.write("!");
  await flush();
  view.stdin.write("a");
  await flush();
  assert.deepEqual(actions, [{ type: "answer_interrupt", interruptId: "i1", payload: { action: "approve", scope: "once" } }]);
  view.unmount();
});

test("首页提供操作引导、模式切换和最近会话，不预置示例问题", async () => {
  const model = {
    ...emptyViewModel(resolveCliConfig({ modelId: "model-1" })),
    surface: "home" as const,
    connected: true,
    loading: false,
    sessions: [{ id: "s1", title: "昨晚的任务", updatedAt: new Date().toISOString(), messageCount: 2 }],
  };
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={model} onAction={(action) => actions.push(action)} />);
  const frame = stripStyles(view.lastFrame() ?? "");
  assert.match(frame, /开始/);
  assert.match(frame, /展开命令选择栏/);
  assert.match(frame, /模式/);
  assert.match(frame, /最近会话/);
  assert.match(frame, /昨晚的任务/);
  assert.doesNotMatch(frame, /快捷提问/);
  assert.doesNotMatch(frame, /今天想完成什么/);
  view.stdin.write("2");
  await flush();
  assert.deepEqual(actions, [{ type: "open_surface", surface: "team" }]);
  view.unmount();
});

test("Shift+Tab 在一级模式之间切换", async () => {
  const actions: TerminalPageAction[] = [];
  const view = render(<TerminalPage model={chatModel()} onAction={(action) => actions.push(action)} />);
  view.stdin.write("\u001b[Z");
  await flush();
  assert.deepEqual(actions, [{ type: "open_surface", surface: "doctor" }]);
  view.unmount();
});

function chatModel() {
  return {
    ...emptyViewModel(resolveCliConfig({ modelId: "model-1" })),
    surface: "chat" as const,
    connected: true,
    loading: false,
  };
}

/** Ink 会在选中项上叠加反显序列，断言前先剥离样式，避免匹配到控制字符。 */
function stripStyles(frame: string): string {
  return frame.replace(/\u001B\[[0-9;]*m/g, "");
}

async function flush(): Promise<void> {
  // Ink 需要额外时间区分裸 ESC 与转义序列，等待过短会漏掉 Esc 键。
  await new Promise((resolve) => setTimeout(resolve, 60));
}
