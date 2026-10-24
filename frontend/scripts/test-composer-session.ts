import assert from "node:assert/strict";
import { resolveComposerSession } from "../src/composer-session";
import type { SessionSummary } from "../src/types";

let created = 0;
let genericIds = 0;
const createId = () => { genericIds += 1; return "generic-session"; };
const createProject = async (projectId: string): Promise<SessionSummary> => {
  created += 1;
  return { id: "project-session", title: "新对话", projectId, updatedAt: new Date().toISOString(), messageCount: 0 };
};
assert.deepEqual(await resolveComposerSession(null, null, createProject, createId), { id: "generic-session" });
const project = await resolveComposerSession(null, "selected-project", createProject, createId);
assert.equal(project.id, "project-session");
assert.equal(project.created?.projectId, "selected-project");
assert.equal(genericIds, 1, "项目会话不能生成通用 thread id");
assert.deepEqual(await resolveComposerSession("existing", "another-project", createProject, createId), { id: "existing" });
assert.equal(created, 1, "后续消息必须复用原会话，不能新建或换绑");
await assert.rejects(resolveComposerSession(null, "broken-project", async () => { throw new Error("创建失败"); }, createId), /创建失败/);
assert.equal(genericIds, 1, "项目创建失败时不能降级到通用工作区");
console.log("composer project session routing: passed");
