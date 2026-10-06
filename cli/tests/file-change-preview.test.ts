import assert from "node:assert/strict";
import test from "node:test";
import stripAnsi from "strip-ansi";
import {
  formatFileChangePlain,
  parseFileChange,
  shortPath,
  takeDiffLines,
} from "../src/tools/file-change-preview.js";

test("CLI 解析工具结果中的 hunks", () => {
  const change = parseFileChange(
    "Edit",
    JSON.stringify({ file_path: "/tmp/workspace/sort.py", old_string: "a", new_string: "b" }),
    JSON.stringify({
      ok: true,
      path: "/tmp/workspace/sort.py",
      hunks: [{
        oldStart: 4,
        oldCount: 3,
        newStart: 4,
        newCount: 3,
        lines: [
          { kind: "ctx", text: "keep", oldLine: 4, newLine: 4 },
          { kind: "del", text: "a", oldLine: 5 },
          { kind: "add", text: "b", newLine: 5 },
        ],
      }],
    }),
  );
  assert.ok(change);
  assert.equal(change.added, 1);
  assert.equal(change.removed, 1);
  assert.equal(shortPath(change.path), "workspace/sort.py");
  assert.equal(takeDiffLines(change, 16).lines.length, 3);
});

test("无 hunk 时从 Edit 参数回退出行级 diff", () => {
  const change = parseFileChange("Edit", JSON.stringify({
    file_path: "foo.ts",
    old_string: "a = 1\nb = 2",
    new_string: "a = 1\nb = 3",
  }));
  assert.ok(change);
  assert.equal(change.added, 1);
  assert.equal(change.removed, 1);
});

test("纯文本 diff 含路径统计和单列行号", () => {
  const change = parseFileChange(
    "Edit",
    "{}",
    JSON.stringify({
      path: "n.py",
      hunks: [{
        oldStart: 10,
        oldCount: 2,
        newStart: 10,
        newCount: 2,
        lines: [
          { kind: "del", text: "old", oldLine: 10 },
          { kind: "add", text: "new", newLine: 10 },
        ],
      }],
    }),
  );
  assert.ok(change);
  const text = stripAnsi(formatFileChangePlain(change, 16));
  assert.match(text, /╭─ Edit {2}n\.py {2}\+1 -1/);
  assert.match(text, /│ .*10 - old/);
  assert.match(text, /│ .*10 \+ new/);
  assert.match(text, /╰─/);
  assert.doesNotMatch(text, /@@ /);
});

test("窄终端会截断过长的 diff 行", () => {
  const long = "x".repeat(80);
  const change = parseFileChange(
    "Edit",
    "{}",
    JSON.stringify({
      path: "wide.py",
      hunks: [{
        oldStart: 1,
        oldCount: 1,
        newStart: 1,
        newCount: 1,
        lines: [{ kind: "add", text: long, newLine: 1 }],
      }],
    }),
  );
  assert.ok(change);
  const text = stripAnsi(formatFileChangePlain(change, 16, 40));
  assert.ok([...text.split("\n")].every((line) => line.length <= 40));
  assert.doesNotMatch(text, new RegExp(long));
});
