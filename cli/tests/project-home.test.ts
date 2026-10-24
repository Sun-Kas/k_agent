import assert from "node:assert/strict";
import test from "node:test";
import { deleteSessionWarning, orderProjects, sessionMeta, sessionsInProject, startChoices, workspaceIsShared } from "../src/terminal-page/project-home.js";
import type { ProjectSummary, SessionSummary } from "../src/protocol/index.js";

const project = (id: string, extra: Partial<ProjectSummary> = {}): ProjectSummary => ({
  id,
  name: id,
  rootPath: `/repo/${id}`,
  sessionCount: 0,
  createdAt: "2026-10-01T00:00:00.000Z",
  updatedAt: "2026-10-01T00:00:00.000Z",
  ...extra,
});

test("置顶项目排在前面，其余按更新时间", () => {
  const ordered = orderProjects([
    project("old", { updatedAt: "2026-10-01T00:00:00.000Z" }),
    project("new", { updatedAt: "2026-10-20T00:00:00.000Z" }),
    project("pin", { updatedAt: "2026-10-02T00:00:00.000Z" }),
  ], ["pin"]);
  assert.deepEqual(ordered.map((item) => item.id), ["pin", "new", "old"]);
});

test("Git 项目提供目录、隔离 Worktree 和带当前改动的 Worktree", () => {
  assert.deepEqual(startChoices(project("plain")).map((item) => item.id), ["local"]);
  assert.deepEqual(startChoices(project("git", { isGitRepository: true })).map((item) => item.startingState), ["default", "default", "working-tree"]);
});

test("会话列表只保留当前项目，并标出共享目录和删除后果", () => {
  const sessions: SessionSummary[] = [
    { id: "a", title: "甲", updatedAt: "2026-10-02T00:00:00.000Z", messageCount: 1, projectId: "p" },
    { id: "b", title: "乙", updatedAt: "2026-10-03T00:00:00.000Z", messageCount: 2, projectId: "other" },
  ];
  assert.deepEqual(sessionsInProject(sessions, "p").map((item) => item.id), ["a"]);
  const local = { kind: "local" as const, cwd: "/repo", projectId: "p", detached: false };
  assert.equal(workspaceIsShared(local, sessions, "a"), true);
  assert.equal(workspaceIsShared({ ...local, kind: "worktree", cwd: "/wt" }, sessions, "a"), false);
  assert.match(deleteSessionWarning(sessions[0]!), /Worktree 文件会保留/);
  assert.match(deleteSessionWarning({ id: "c", title: "", updatedAt: "", messageCount: 0 }), /永久删除/);
  assert.match(sessionMeta({ ...sessions[0]!, environment: { ...local, branchRef: "main" } }, [project("p", { name: "仓库" })]), /Local main · 仓库/);
});
