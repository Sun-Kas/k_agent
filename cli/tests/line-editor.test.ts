import assert from "node:assert/strict";
import test from "node:test";
import {
  createLineEditor,
  deleteBackward,
  deleteForward,
  deleteToLineStart,
  deleteWordBackward,
  graphemeAtCursor,
  insertText,
  moveLeft,
  moveRight,
  moveToEnd,
  moveToStart,
  moveWordLeft,
  textAfterCursor,
  textBeforeCursor,
} from "../src/terminal-page/line-editor.js";

test("光标以 grapheme 为单位移动，中文不会被切成半个字符", () => {
  let state = createLineEditor("你好世界");
  assert.equal(state.cursor, 4);
  state = moveLeft(moveLeft(state));
  assert.equal(textBeforeCursor(state), "你好");
  assert.equal(graphemeAtCursor(state), "世");
  assert.equal(textAfterCursor(state), "界");
  state = insertText(state, "中");
  assert.equal(state.value, "你好中世界");
  assert.equal(state.cursor, 3);
});

test("退格与前向删除只影响一个完整字符", () => {
  let state = createLineEditor("a👩‍💻b");
  state = moveLeft(state);
  state = deleteBackward(state);
  assert.equal(state.value, "ab");
  state = moveToStart(state);
  state = deleteForward(state);
  assert.equal(state.value, "b");
  assert.equal(state.cursor, 0);
});

test("Ctrl+W 删除光标左侧一个完整词，Ctrl+U 删到行首", () => {
  const state = createLineEditor("npm run build");
  assert.equal(deleteWordBackward(state).value, "npm run ");
  assert.equal(deleteToLineStart(state).value, "");
  assert.equal(deleteToLineStart(state).cursor, 0);
});

test("按词移动跳过连续空白", () => {
  const state = moveWordLeft(createLineEditor("检查  项目 结构"));
  assert.equal(textBeforeCursor(state), "检查  项目 ");
});

test("光标越界时被夹紧到合法范围", () => {
  const state = createLineEditor("abc", 99);
  assert.equal(state.cursor, 3);
  assert.equal(moveRight(state).cursor, 3);
  assert.equal(moveToEnd(moveToStart(state)).cursor, 3);
});
