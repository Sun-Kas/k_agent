import assert from "node:assert/strict";
import test from "node:test";
import { imeCursorPosition, terminalCursorOffset } from "../src/terminal-page/use-ime-cursor.js";

test("中文按终端列宽计算光标偏移", () => {
  assert.deepEqual(terminalCursorOffset("❯ 你好", 80), { x: 6, y: 0 });
});

test("零宽 caret 只要有布局坐标仍应作为输入法锚点", () => {
  assert.deepEqual(
    imeCursorPosition({ x: 12, y: 20, width: 0, height: 1 }, ""),
    { x: 12, y: 20 },
  );
});

test("尚未布局时不把输入法光标清到屏幕底部", () => {
  assert.equal(imeCursorPosition({ x: 0, y: 0, width: 0, height: 0 }, "❯ "), undefined);
});

test("在已测量的输入行上，光标落在 prompt 与已输入文本之后", () => {
  assert.deepEqual(
    imeCursorPosition({ x: 2, y: 18, width: 80, height: 1 }, "❯ 你好"),
    { x: 8, y: 18 },
  );
});

test("同一行原点下文本变长时立即算出新插入点，不沿用旧列", () => {
  const line = { x: 2, y: 18, width: 80, height: 1 };
  const before = imeCursorPosition(line, "❯ 你好");
  const after = imeCursorPosition(line, "❯ 你好大");
  assert.equal(before?.x, 8);
  assert.equal(after?.x, 10);
  assert.equal(after?.y, before?.y);
});

