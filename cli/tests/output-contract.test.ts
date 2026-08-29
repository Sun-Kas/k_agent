import assert from "node:assert/strict";
import test from "node:test";
import { safeJson } from "../src/output/json.js";
import { sanitizeTerminalContent } from "../src/output/sanitize.js";
import { EXIT_CODES } from "../src/output/exit-codes.js";

test("终端输出清理 ANSI、OSC 和危险控制字符", () => {
  const value = "\u001b[31m红色\u001b[0m\u001b]0;伪标题\u0007\u0000正文";
  assert.equal(sanitizeTerminalContent(value), "红色正文");
  assert.equal(JSON.parse(safeJson({ value })).value, "红色正文");
});

test("公开退出码保持稳定", () => {
  assert.deepEqual(EXIT_CODES, { success: 0, agentError: 1, inputRequired: 2, connectionError: 3, configError: 4, interrupted: 130 });
});
