import assert from "node:assert/strict";
import test from "node:test";
import {
  formatMcpListLines,
  parseMcpSlashArgs,
  sanitizeMcpServersForSave,
  setMcpEnabled,
  toolsFromCapabilities,
} from "../src/application/mcp-config.js";
import type { McpServerConfig } from "../src/protocol/index.js";

const servers: McpServerConfig[] = [
  { id: "calendar", name: "日历", enabled: true, type: "stdio", args: [], env: {}, status: "connected", toolCount: 3 },
  { id: "hidden", name: "hidden", enabled: false, type: "http", args: [], env: {}, url: "https://example" },
];

test("/mcp 参数解析 enable/disable/reload", () => {
  assert.deepEqual(parseMcpSlashArgs(""), { kind: "menu" });
  assert.deepEqual(parseMcpSlashArgs("reload"), { kind: "reload" });
  assert.deepEqual(parseMcpSlashArgs("enable"), { kind: "enable", target: "all" });
  assert.deepEqual(parseMcpSlashArgs("disable calendar"), { kind: "disable", target: "calendar" });
  assert.equal(parseMcpSlashArgs("nope").kind, "unknown");
});

test("按 id 或 all 切换 enabled，找不到则 missing", () => {
  const one = setMcpEnabled(servers, "calendar", false);
  assert.equal(one.changed[0]?.enabled, false);
  assert.equal(one.servers.find((item) => item.id === "calendar")?.enabled, false);
  const allOn = setMcpEnabled(servers, "all", true);
  assert.equal(allOn.changed.length, 1);
  assert.equal(allOn.changed[0]?.id, "hidden");
  assert.equal(setMcpEnabled(servers, "missing", true).missing, true);
});

test("保存载荷去掉运行时状态并按 transport 裁剪", () => {
  const saved = sanitizeMcpServersForSave(servers);
  assert.equal("status" in saved[0]!, false);
  assert.equal(saved[1]?.url, "https://example");
  assert.equal(saved[1]?.command, undefined);
});

test("列表行包含开关和连接状态", () => {
  const text = formatMcpListLines(servers).join("\n");
  assert.match(text, /on +connected +日历/);
  assert.match(text, /off/);
});

test("capabilities 同时接受 snake 与 camel 的 server id", () => {
  const tools = toolsFromCapabilities({
    tools: [
      { server_id: "calendar", name: "create_event", description: "x" },
      { serverId: "hidden", name: "noop" },
    ],
  });
  assert.deepEqual(tools.map((item) => item.serverId), ["calendar", "hidden"]);
});
