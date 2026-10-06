import React, { useRef } from "react";
import assert from "node:assert/strict";
import test from "node:test";
import { EventEmitter } from "node:events";
import { Box, Text, render, type DOMElement } from "ink";
import { useImeCursor } from "../src/terminal-page/use-ime-cursor.js";

/**
 * 真实光标的行号只有在写进终端的转义序列里才能验证：Ink 按「帧尾多一行」计算上移量，
 * 而帧占满视口时它不会写那一行。这里直接解析 stdout，锁住两种帧高下的落点。
 */
test("输入行的真实光标在满屏帧和非满屏帧都落在同一行", async () => {
  const roomy = await renderProbe({ rows: 40, filler: 10 });
  const fullscreen = await renderProbe({ rows: 10, filler: 10 });
  // 帧共 11 行，输入行是第 10 行（0 起）。
  assert.deepEqual(roomy, { row: 10, column: 4 });
  assert.deepEqual(fullscreen, { row: 10, column: 4 });
});

function Probe({ filler }: { filler: number }): React.ReactElement {
  const lineRef = useRef<DOMElement | null>(null);
  useImeCursor(lineRef, "> ab", true);
  return (
    <Box flexDirection="column">
      {Array.from({ length: filler }, (_, index) => (
        <Text key={index}>line-{index}</Text>
      ))}
      <Box ref={lineRef} width="100%"><Text>{"> ab"}</Text></Box>
    </Box>
  );
}

async function renderProbe({ rows, filler }: { rows: number; filler: number }): Promise<{ row: number; column: number }> {
  const stdout = new FakeStdout(rows);
  const app = render(<Probe filler={filler} />, {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stdin: fakeStdin(),
    patchConsole: false,
    exitOnCtrlC: false,
  });
  await new Promise((resolve) => setTimeout(resolve, 60));
  app.unmount();
  return cursorLanding(stdout.chunks);
}

/**
 * 重放首帧：先算写完帧正文后光标physically停在哪一行，再套用 Ink 追加的光标序列。
 * 关键差异就在帧尾那个换行——满屏帧没有它，光标停在最后一行本身。
 */
function cursorLanding(chunks: string[]): { row: number; column: number } {
  const stream = chunks.join("");
  const frame = stream.slice(stream.indexOf("line-0"));
  const bodyEnd = frame.indexOf("> ab") + "> ab".length;
  let row = countLineBreaks(frame.slice(0, bodyEnd));
  let column = "> ab".length;
  // 光标序列止于「显示光标」，后面是 unmount 的清屏，不参与落点计算。
  const suffix = frame.slice(bodyEnd).split("\u001B[?25h")[0] ?? "";
  if (suffix.startsWith("\n")) {
    row += 1;
    column = 0;
  }
  for (const [, amount, command] of suffix.matchAll(/\u001B\[(\d*)([ABG])/g)) {
    const steps = Number(amount || "1");
    if (command === "A") row -= steps;
    if (command === "B") row += steps;
    if (command === "G") column = steps - 1;
  }
  return { row, column };
}

function countLineBreaks(text: string): number {
  return (text.match(/\n/g) ?? []).length;
}

function fakeStdin(): NodeJS.ReadStream {
  const stdin = new EventEmitter() as unknown as NodeJS.ReadStream & { isTTY: boolean };
  stdin.isTTY = true;
  Object.assign(stdin, {
    setRawMode: () => stdin,
    setEncoding: () => stdin,
    read: () => null,
    resume: () => stdin,
    pause: () => stdin,
    ref: () => stdin,
    unref: () => stdin,
  });
  return stdin;
}

class FakeStdout extends EventEmitter {
  isTTY = true;
  columns = 80;
  rows: number;
  chunks: string[] = [];

  constructor(rows: number) {
    super();
    this.rows = rows;
  }

  write(chunk: string): boolean {
    this.chunks.push(chunk);
    return true;
  }
}
