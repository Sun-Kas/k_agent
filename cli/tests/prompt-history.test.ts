import assert from "node:assert/strict";
import test from "node:test";
import { pushPromptHistory, promptHistoryText } from "../src/terminal-page/prompt-history.js";

test("连续相同提示不重复入历史", () => {
  const once = pushPromptHistory([], "检查项目");
  assert.deepEqual(pushPromptHistory(once, "检查项目"), ["检查项目"]);
  assert.deepEqual(pushPromptHistory(once, "  "), ["检查项目"]);
});

test("cursor -1 回到当前草稿，0 是最近一条已发送内容", () => {
  const history = pushPromptHistory(pushPromptHistory([], "第一句"), "第二句");
  assert.equal(promptHistoryText(history, -1, "草稿"), "草稿");
  assert.equal(promptHistoryText(history, 0, "草稿"), "第二句");
  assert.equal(promptHistoryText(history, 1, "草稿"), "第一句");
  assert.equal(promptHistoryText(history, 2, "草稿"), undefined);
  assert.equal(promptHistoryText(history, -2, "草稿"), undefined);
});
