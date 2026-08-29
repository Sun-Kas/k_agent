import assert from "node:assert/strict";
import test from "node:test";
import { terminalLayout } from "../src/terminal-page/design.js";

test("终端宽度阈值与设计文档一致", () => {
  assert.equal(terminalLayout(160), "wide");
  assert.equal(terminalLayout(120), "standard");
  assert.equal(terminalLayout(90), "compact");
  assert.equal(terminalLayout(60), "minimum");
});
