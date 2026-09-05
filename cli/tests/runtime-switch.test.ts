import assert from "node:assert/strict";
import test from "node:test";
import {
  parseMenuOrSetArgs,
  selectAgent,
  selectModel,
  selectPermission,
} from "../src/application/runtime-switch.js";
import { resolveCliConfig } from "../src/config/index.js";
import type { RuntimeChoice } from "../src/terminal-page/types.js";

const models: RuntimeChoice[] = [
  { id: "sonnet", name: "Sonnet", enabled: true },
  { id: "opus", name: "Opus", enabled: false },
];
const agents: RuntimeChoice[] = [
  { id: "k_agent", name: "K Agent", enabled: true },
  { id: "claude_code", name: "Claude Code", enabled: false },
];

test("/model 参数可省略动词", () => {
  assert.deepEqual(parseMenuOrSetArgs(""), { kind: "menu" });
  assert.deepEqual(parseMenuOrSetArgs("sonnet"), { kind: "set", target: "sonnet" });
  assert.deepEqual(parseMenuOrSetArgs("use opus"), { kind: "set", target: "opus" });
  assert.equal(parseMenuOrSetArgs("set").kind, "unknown");
});

test("只允许切换已启用模型", () => {
  const config = resolveCliConfig({ modelId: "sonnet" });
  const ok = selectModel(config, models, "Sonnet");
  assert.equal("config" in ok && ok.config.modelId, "sonnet");
  const disabled = selectModel(config, models, "opus");
  assert.equal("error" in disabled, true);
  assert.equal("error" in selectModel(config, models, "missing"), true);
});

test("权限只接受 default 或 full_access", () => {
  const config = resolveCliConfig();
  const ok = selectPermission(config, "full_access");
  assert.equal("config" in ok && ok.config.permissionMode, "full_access");
  assert.equal("error" in selectPermission(config, "yolo"), true);
});

test("不可用 Agent 不能选", () => {
  const config = resolveCliConfig();
  assert.equal("error" in selectAgent(config, agents, "claude_code"), true);
  const ok = selectAgent(config, agents, "k_agent");
  assert.equal("config" in ok && ok.config.agentKind, "k_agent");
});
