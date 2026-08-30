import assert from "node:assert/strict";
import test from "node:test";
import {
  filterSlashCommands,
  SLASH_COMMANDS,
  slashCommandAnnotation,
  slashCommandAvailability,
  slashQuery,
} from "../src/terminal-page/slash-catalog.js";
import { resolveCliConfig } from "../src/config/index.js";
import { emptyViewModel } from "../src/application/view-model.js";
import type { TerminalPageViewModel } from "../src/terminal-page/index.js";

test("只有以 / 开头且未输入参数时才展开选择栏", () => {
  assert.equal(slashQuery("/"), "");
  assert.equal(slashQuery("/mod"), "mod");
  assert.equal(slashQuery("/workspace src"), undefined);
  assert.equal(slashQuery("检查项目"), undefined);
  assert.equal(slashQuery(""), undefined);
});

test("过滤结果按匹配质量确定排序，前缀命中优先", () => {
  const matches = filterSlashCommands("st");
  assert.equal(matches[0]?.name, "stop");
  assert.deepEqual(filterSlashCommands("model").map((item) => item.name), ["model"]);
  assert.equal(filterSlashCommands("").length, SLASH_COMMANDS.length);
  assert.deepEqual(filterSlashCommands("不存在的命令"), []);
});

test("选择栏只声明 application 层已实现的命令", () => {
  const supported = new Set([
    "new", "sessions", "chat", "home", "team", "auto", "automation", "doctor",
    "workspace", "stop", "cancel", "trace", "model", "agent", "mcp", "skill",
    "permissions", "help", "quit",
  ]);
  for (const command of SLASH_COMMANDS) {
    assert.ok(supported.has(command.name), `未实现的命令 /${command.name}`);
  }
});

test("stop 与 cancel 只在运行中可用，且不可用时必须给出原因", () => {
  const stop = command("stop");
  const idle = slashCommandAvailability(stop, model());
  assert.equal(idle.enabled, false);
  assert.equal(idle.reason, "没有运行中的任务");
  assert.equal(slashCommandAvailability(stop, model({ runStatus: "running" })).enabled, true);
  assert.equal(slashCommandAvailability(command("cancel"), model({ runStatus: "running" })).enabled, true);
});

test("workspace 需要已打开 Session，new 在运行中不可用", () => {
  assert.equal(slashCommandAvailability(command("workspace"), model()).enabled, false);
  assert.equal(slashCommandAvailability(command("workspace"), model({ activeSessionId: "s1" })).enabled, true);
  assert.equal(slashCommandAvailability(command("new"), model({ runStatus: "running" })).enabled, false);
});

test("只读查询在运行中和断线时依然可用", () => {
  const busy = model({ runStatus: "running", connected: false });
  for (const name of ["model", "agent", "mcp", "skill", "permissions", "trace", "sessions", "help", "quit"]) {
    assert.equal(slashCommandAvailability(command(name), busy).enabled, true, `/${name} 应保持可用`);
  }
});

test("注解优先展示不可用原因，其次是当前值", () => {
  assert.equal(slashCommandAnnotation(command("stop"), model()), "没有运行中的任务");
  assert.match(slashCommandAnnotation(command("model"), model()), /查看当前模型.*model-1/);
  assert.match(command("mcp").hint, /Web/);
  assert.match(command("skill").hint, /Web/);
  assert.match(command("permissions").hint, /Web/);
});

function command(name: string) {
  const item = SLASH_COMMANDS.find((entry) => entry.name === name);
  assert.ok(item, `目录缺少 /${name}`);
  return item;
}

function model(overrides: {
  runStatus?: TerminalPageViewModel["timeline"]["runStatus"];
  activeSessionId?: string;
  connected?: boolean;
} = {}): TerminalPageViewModel {
  const base = emptyViewModel(resolveCliConfig({ modelId: "model-1" }));
  return {
    ...base,
    connected: overrides.connected ?? true,
    ...(overrides.activeSessionId ? { activeSessionId: overrides.activeSessionId } : {}),
    timeline: { ...base.timeline, runStatus: overrides.runStatus ?? "idle" },
  };
}
