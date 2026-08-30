import assert from "node:assert/strict";
import test from "node:test";
import { canFlushPromptQueue, estimateTimelineItemLines, runningActivityHint, splitSettledTimeline, visibleTimelineByBudget, visibleTimelineSlice } from "../src/terminal-page/repl-view.js";
import { formatWorkspaceContext } from "../src/terminal-page/workspace-context.js";
import type { TimelineItem } from "../src/application/event-projector.js";

test("运行中和等待输入时不冲刷排队提示", () => {
  assert.equal(canFlushPromptQueue("idle"), true);
  assert.equal(canFlushPromptQueue("complete"), true);
  assert.equal(canFlushPromptQueue("running"), false);
  assert.equal(canFlushPromptQueue("waiting_input"), false);
});

test("滚动窗口从底部截取，并报告被藏住的条数", () => {
  const items = [1, 2, 3, 4, 5];
  assert.deepEqual(visibleTimelineSlice(items, 0, 3), { items: [3, 4, 5], offset: 0, hiddenAbove: 2 });
  assert.deepEqual(visibleTimelineSlice(items, 2, 3), { items: [1, 2, 3], offset: 2, hiddenAbove: 0 });
});

test("按行高从底部取条目，长回复不会把更早的轮次挤出窗口", () => {
  const long: TimelineItem = {
    id: "a1",
    sequence: 2,
    kind: "text",
    content: "第一轮回答\n".repeat(20),
    status: "complete",
  };
  const user2: TimelineItem = { id: "u2", sequence: 3, kind: "user", content: "第二问" };
  const items: TimelineItem[] = [
    { id: "u1", sequence: 1, kind: "user", content: "第一问" },
    long,
    user2,
    { id: "a2", sequence: 4, kind: "text", content: "第二轮很短", status: "complete" },
  ];
  const slice = visibleTimelineByBudget(items, 0, 12, (item) => estimateTimelineItemLines(item, 80, 8, false));
  assert.ok(slice.items.some((item) => item.id === "a2"));
  assert.ok(slice.items.some((item) => item.kind === "user"));
  assert.ok(slice.hiddenAbove >= 0);
});

test("已完成前缀进入滚动区，进行中的条目留在动态区", () => {
  const items: TimelineItem[] = [
    { id: "u1", sequence: 1, kind: "user", content: "第一问" },
    { id: "a1", sequence: 2, kind: "text", content: "第一轮回答", status: "complete" },
    { id: "u2", sequence: 3, kind: "user", content: "第二问" },
    { id: "t2", sequence: 4, kind: "thinking", title: "思考过程", content: "", status: "active" },
  ];
  const split = splitSettledTimeline(items);
  assert.deepEqual(split.settled.map((item) => item.id), ["u1", "a1", "u2"]);
  assert.deepEqual(split.live.map((item) => item.id), ["t2"]);
});

test("排队中的用户输入不算已完成", () => {
  const items: TimelineItem[] = [
    { id: "u1", sequence: 1, kind: "user", content: "已发送" },
    { id: "queued-0", sequence: 2, kind: "user", content: "排队" },
  ];
  const split = splitSettledTimeline(items);
  assert.deepEqual(split.settled.map((item) => item.id), ["u1"]);
  assert.deepEqual(split.live.map((item) => item.id), ["queued-0"]);
});

test("运行提示优先展示当前工具名", () => {
  const items: TimelineItem[] = [
    { id: "t1", sequence: 1, kind: "tool", name: "bash", arguments: "", status: "active" },
  ];
  assert.equal(runningActivityHint(items), "bash");
  assert.equal(runningActivityHint([]), "正在运行");
});

test("欢迎框工作区文案只拼目录和分支", () => {
  assert.equal(formatWorkspaceContext({ folder: "k_agent", cwd: "/tmp/k_agent" }), "k_agent");
  assert.equal(formatWorkspaceContext({ folder: "k_agent", cwd: "/tmp/k_agent", branch: "main" }), "k_agent · main");
});
