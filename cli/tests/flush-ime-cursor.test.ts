import assert from "node:assert/strict";
import test from "node:test";
import { revealImeCursor } from "../src/terminal-page/flush-ime-cursor.js";

test("只丢掉单独写出的同步更新括号，光标指令原样保留", () => {
  const chunks: Array<string | Uint8Array> = [];
  const stdout = {
    write(chunk: string | Uint8Array) {
      chunks.push(chunk);
      return true;
    },
  } as NodeJS.WriteStream;
  revealImeCursor(stdout);
  stdout.write("\u001b[?2026h");
  stdout.write("frame\n");
  stdout.write("\u001b[?2026l");
  stdout.write("\u001b[3A\u001b[5G\u001b[?25h");
  stdout.write("keep \u001b[?2026h inside");
  assert.deepEqual(chunks, ["frame\n", "\u001b[3A\u001b[5G\u001b[?25h", "keep \u001b[?2026h inside"]);
});
