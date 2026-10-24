import React from "react";
import assert from "node:assert/strict";
import test from "node:test";
import { readFile, mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { render } from "ink-testing-library";
import { WORKBENCH_OPERATIONS, operationRequest, initialOperationValues } from "../src/application/workbench-catalog.js";
import { AccessLayerClient } from "../src/client/access-layer-client.js";
import { WorkbenchScreen, formatWorkbenchResult, wrapWorkbenchLines } from "../src/terminal-page/screens/WorkbenchScreen.js";
import { ChatController } from "../src/application/chat-controller.js";
import { emptyViewModel } from "../src/application/view-model.js";
import { resolveCliConfig } from "../src/config/index.js";
import { createRunInput } from "../src/application/run-input.js";
import { readAttachment } from "../src/application/workbench-local.js";
import type { TerminalPageAction } from "../src/terminal-page/types.js";

const operation = (id: string) => { const op = WORKBENCH_OPERATIONS.find((item) => item.id === id); assert.ok(op); return op; };
const flush = () => new Promise((resolve) => setTimeout(resolve, 45));

test("工作台路径与查询编码，业务字段保持 Web 请求语义", () => {
  assert.deepEqual(operationRequest(operation("diff"), { sessionId: "a/b", path: "中文 x&y.ts" }), { path: "/api/sessions/a%2Fb/diff?path=%E4%B8%AD%E6%96%87+x%26y.ts" });
  assert.deepEqual(operationRequest(operation("project-session"), { projectId: "p", environment: "worktree", startingState: '{"type":"working-tree"}' }), { path: "/api/projects/p/sessions", body: { environment: "worktree", startingState: { type: "working-tree" } } });
  const fork = operation("fork");
  assert.deepEqual(operationRequest(fork, { ...initialOperationValues(fork, "s"), throughMessageId: "m1" }).body, { throughMessageId: "m1", environment: "same-directory" });
  assert.throws(() => operationRequest(operation("runtime"), { ...initialOperationValues(operation("runtime")), modelId: "m", skillIds: '{}' }), /字符串数组/);
  assert.throws(() => operationRequest(operation("handoff"), { sessionId: "s", target: "bad" }), /请选择/);
});

test("工作台覆盖全部 Web API 导出函数，新增功能必须更新对照", async () => {
  const mapping = JSON.parse(await readFile(new URL("../../docs/features/web-cli-api-map.json", import.meta.url), "utf8")) as Record<string, Record<string, string>>;
  const existing = new Set(WORKBENCH_OPERATIONS.map((op) => op.id));
  const builtins = new Set(["stream", "stop", "cancel", "team-stream"]);
  for (const [file, functions] of Object.entries(mapping)) {
    const source = await readFile(new URL(`../../frontend/src/${file}`, import.meta.url), "utf8");
    const exports = [...source.matchAll(/export\s+(?:async\s+)?(?:function|const)\s+(\w+)/g)].map((match) => match[1]!).filter((name) => name !== "MARKETPLACE_PAGE_SIZE");
    assert.deepEqual(Object.keys(functions).sort(), exports.sort(), `${file} 出现新增或删除的 Web 功能，请同步 CLI 和清单`);
    for (const target of Object.values(functions)) assert.ok(existing.has(target) || builtins.has(target), `未实现 ${target}`);
  }
});

test("HTTP 客户端发送 fork 锚点、处理 204 和服务端错误", async () => {
  const previous = globalThis.fetch;
  const calls: Array<{ url: string; body: unknown }> = [];
  try {
    globalThis.fetch = async (url, init) => {
      calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return new Response(JSON.stringify({ id: "branch" }), { headers: { "Content-Type": "application/json" } });
    };
    const client = new AccessLayerClient("http://localhost:3001");
    await client.forkSession("s/x", { throughRunId: "r", environment: "worktree" });
    assert.deepEqual(calls[0], { url: "http://localhost:3001/api/sessions/s%2Fx/fork", body: { throughRunId: "r", environment: "worktree" } });
    globalThis.fetch = async () => new Response(null, { status: 204 });
    assert.equal(await client.workbenchRequest("/api/scheduled-tasks/x", "DELETE"), undefined);
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: "工作区不干净" }), { status: 400 });
    await assert.rejects(client.workbenchRequest("/api/sessions/s/handoff", "POST", { target: "local" }), /工作区不干净/);
  } finally { globalThis.fetch = previous; }
});

test("工作台过滤、表单编辑、确认提交与返回不污染聊天草稿", async () => {
  const actions: TerminalPageAction[] = [];
  const model = emptyViewModel(resolveCliConfig());
  model.activeSessionId = "s";
  const view = render(<WorkbenchScreen model={model} onAction={(action) => actions.push(action)} />);
  try {
    view.stdin.write("handoff"); await flush();
    assert.match(view.lastFrame() ?? "", /切换 Local/);
    view.stdin.write("\r"); await flush();
    assert.match(view.lastFrame() ?? "", /会话 ID/);
    view.stdin.write("\r"); await flush();
    assert.match(view.lastFrame() ?? "", /目标环境/);
    view.stdin.write("\u0015"); view.stdin.write("local"); await flush();
    view.stdin.write("\u0013"); await flush();
    assert.match(view.lastFrame() ?? "", /确认执行/);
    assert.equal(actions.length, 0);
    view.stdin.write("\r"); await flush();
    assert.deepEqual(actions, [{ type: "workbench_execute", operationId: "handoff", values: { sessionId: "s", target: "local" } }]);
    view.stdin.write("\u001b"); await flush();
    assert.match(view.lastFrame() ?? "", /目标环境/);
  } finally { view.unmount(); }
});

test("文件与 diff 多行原文可读，过滤控制序列并按中文宽度分页", () => {
  assert.equal(formatWorkbenchResult({ path: "x", content: "甲\n乙" }), '{\n  "path": "x"\n}\n\n甲\n乙');
  assert.doesNotMatch(formatWorkbenchResult("x\u001b]52;c;evil\u0007y\u001b[2J"), /evil|\u001b/);
  assert.deepEqual(wrapWorkbenchLines("中文abcdef\n第二行", 6), ["中文ab", "cdef", "第二行"]);
});

test("图片校验与运行选项传递到公开 forwardedProps", async () => {
  const dir = await mkdtemp(join(tmpdir(), "k-cli-attachment-"));
  try {
    await writeFile(join(dir, "x.png"), Buffer.from([1, 2, 3]));
    const attachment = await readAttachment(join(dir, "x.png"));
    assert.equal(attachment.dataUrl, "data:image/png;base64,AQID");
    await assert.rejects(readAttachment(join(dir, "x.txt")), /图片或视频/);
    const config = { ...resolveCliConfig(), networkAccess: true, resumeSessionId: "resume", claudeAutoApproveTools: ["Read"] };
    const run = createRunInput("test", config);
    assert.equal(run.input.forwardedProps.agentOptions?.networkAccess, true);
    assert.equal(run.input.forwardedProps.agentOptions?.resumeSessionId, "resume");
    assert.deepEqual(run.input.forwardedProps.agentOptions?.claudeAutoApproveTools, ["Read"]);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("控制器在终端执行项目会话创建并显示服务端 cwd", async () => {
  let created: unknown;
  const environment = { kind: "worktree", cwd: "/remote/worktree", projectId: "p", branchRef: null, detached: true };
  const client = {
    health: async () => ({ ok: true, agentBackendOk: true, localToolCount: 0, mcpToolCount: 0 }),
    catalog: async () => ({ mcpServers: [], skills: [] }), agents: async () => ({ agents: [] }),
    models: async () => [], listSessions: async () => [{ id: "s", title: "项目会话", environment }],
    listTeams: async () => [], listScheduledTasks: async () => [],
    getSession: async () => ({ sessionId: "s", events: [], environment }),
    workbenchRequest: async (path: string, method: string, body: unknown) => { created = { path, method, body }; return { id: "s" }; },
  } as unknown as AccessLayerClient;
  const view = render(<ChatController client={client} initialConfig={resolveCliConfig()} />);
  try {
    await flush(); view.stdin.write("\u0007"); await flush();
    view.stdin.write("project-session"); await flush(); view.stdin.write("\r"); await flush();
    view.stdin.write("p"); await flush(); view.stdin.write("\u0013"); await flush(); view.stdin.write("\r"); await flush();
    assert.deepEqual(created, { path: "/api/projects/p/sessions", method: "POST", body: { environment: "auto", startingState: { type: "default" } } });
    assert.match(view.lastFrame() ?? "", /\/remote\/worktree/);
  } finally { view.unmount(); }
});

test("本轮能力空数组表示不选，与全局启用状态分离", async () => {
  const { applyMcpServersToRuntime } = await import("../src/application/mcp-config.js");
  const { applySkillsToRuntime } = await import("../src/application/skill-config.js");
  const base = emptyViewModel(resolveCliConfig()).runtime;
  assert.equal(applyMcpServersToRuntime(base, [{ id: "a", enabled: true }]).mcpCount, 0);
  assert.equal(applySkillsToRuntime(base, [{ id: "a", name: "a", enabled: true }]).skillCount, 0);
  assert.equal(applyMcpServersToRuntime({ ...base, selectedMcpIds: ["a"] }, [{ id: "a", enabled: true }]).mcpCount, 1);
});

test("切换外部 Agent 后采用其模型目录，切回内置 Agent 恢复内置模型目录", async () => {
  const { applyRuntimeToSummary, choicesFromAgents, selectAgent } = await import("../src/application/runtime-switch.js");
  const config = resolveCliConfig({ modelId: "local" });
  const agents = choicesFromAgents([{ kind: "codex", name: "Codex", available: true, defaultModelId: "external", models: [{ id: "external", name: "外部模型" }] }]);
  const switched = selectAgent(config, agents, "codex");
  assert.ok("config" in switched);
  assert.equal(switched.config.modelId, "external");
  const runtime = { ...emptyViewModel(config).runtime, agents, modelCatalog: [{ id: "local", name: "内置模型", enabled: true }] };
  assert.equal(applyRuntimeToSummary(runtime, switched.config).models[0]?.id, "external");
  assert.equal(applyRuntimeToSummary(runtime, config).models[0]?.id, "local");
});
