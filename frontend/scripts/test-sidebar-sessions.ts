import assert from "node:assert/strict";
import { selectSidebarSessions } from "../src/sidebar-sessions";
import type { SessionSummary } from "../src/types";
import { parseProjectPins, toggleProjectPin } from "../src/project-pins";

const sessions: SessionSummary[] = [
  { id: "general", title: "Python 排序", updatedAt: "2026-10-20T12:00:00Z", messageCount: 2 },
  { id: "project-a", title: "Python 项目重构", updatedAt: "2026-10-23T12:00:00Z", projectId: "a", messageCount: 4 },
  { id: "project-b", title: "项目设计", updatedAt: "2026-10-22T12:00:00Z", projectId: "b", messageCount: 1 },
  { id: "orphaned", title: "已移除项目的会话", updatedAt: "2026-10-21T12:00:00Z", projectId: "removed", messageCount: 2 }
];
const original = structuredClone(sessions);

// 全部最近会话混排，不能丢失普通会话或关联了已移除项目的历史会话。
assert.deepEqual(selectSidebarSessions(sessions, "", null).map((session) => session.id), ["project-a", "project-b", "orphaned", "general"]);
assert.deepEqual(selectSidebarSessions(sessions, "", "a").map((session) => session.id), ["project-a"]);
assert.deepEqual(selectSidebarSessions(sessions, " PYTHON ", null).map((session) => session.id), ["project-a", "general"]);
assert.deepEqual(selectSidebarSessions(sessions, "排序", "a"), []);
assert.deepEqual(selectSidebarSessions(sessions, "", "empty-project"), []);
assert.deepEqual(sessions, original, "排序不能改写会话或原列表顺序");
console.log("sidebar session selection: passed");

assert.deepEqual(parseProjectPins('invalid'), []);
assert.deepEqual(parseProjectPins('{"id":"a"}'), []);
assert.deepEqual(parseProjectPins('["a",null,"a",42,"","b"]'), ["a", "b"]);
const pins = toggleProjectPin(["b"], "a");
assert.deepEqual(parseProjectPins(JSON.stringify(pins)), ["b", "a"], "刷新后保留置顶顺序");
assert.deepEqual(toggleProjectPin(pins, "b"), ["a"]);
assert.deepEqual(pins, ["b", "a"], "取消置顶不修改旧的状态数组");
console.log("project pin persistence: passed");
