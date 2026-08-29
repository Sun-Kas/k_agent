import assert from "node:assert/strict";
import test from "node:test";
import { RESTORE_TERMINAL_SEQUENCE, restoreTerminalAfterTui } from "../src/output/restore-terminal.js";
import { restoreTerminalAfterTui as restoreFromDevLocal } from "../scripts/dev-local.mjs";

test("TUI 退出序列先显示光标再移到底部换行", () => {
  assert.match(RESTORE_TERMINAL_SEQUENCE, /^\u001b\[\?25h\u001b\[9999B\u001b\[1G\n$/);
});

test("restoreTerminalAfterTui 只在 TTY stdout 上写入退出序列", () => {
  const chunks: string[] = [];
  const stdout = {
    isTTY: true,
    write(value: string) {
      chunks.push(value);
      return true;
    },
  } as unknown as NodeJS.WriteStream;
  const stdin = { isTTY: false } as NodeJS.ReadStream;
  restoreTerminalAfterTui(stdout, stdin);
  assert.deepEqual(chunks, [RESTORE_TERMINAL_SEQUENCE]);
});

test("dev:local 关闭前提示前使用同一套终端恢复", () => {
  const chunks: string[] = [];
  const stdout = {
    isTTY: true,
    write(value: string) {
      chunks.push(value);
      return true;
    },
  } as unknown as NodeJS.WriteStream;
  restoreFromDevLocal(stdout, { isTTY: false } as NodeJS.ReadStream);
  assert.deepEqual(chunks, [RESTORE_TERMINAL_SEQUENCE]);
});
