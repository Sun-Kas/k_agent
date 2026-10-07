import assert from "node:assert/strict";

import { lineDiff, parseFileChange } from "../src/tools/file-change-preview";

assert.equal(parseFileChange("Bash", "{\"cmd\":\"pwd\"}"), null);
assert.equal(parseFileChange("Edit", "{\"file_path\":\"a.ts\""), null);

const edit = parseFileChange("Edit", JSON.stringify({
  file_path: "backend/foo.py",
  old_string: "a = 1\nb = 2\n",
  new_string: "a = 1\nb = 3\n",
}));
assert.ok(edit);
assert.equal(edit.path, "backend/foo.py");
assert.equal(edit.added, 1);
assert.equal(edit.removed, 1);
assert.deepEqual(edit.hunks[0]?.lines.map((line) => [line.kind, line.text]), [
  ["ctx", "a = 1"],
  ["del", "b = 2"],
  ["add", "b = 3"],
  ["ctx", ""],
]);

const deleted = parseFileChange("Edit", JSON.stringify({
  file_path: "gone.ts",
  old_string: "keep\nremove",
  new_string: "",
}));
assert.ok(deleted);
assert.equal(deleted.added, 0);
assert.equal(deleted.removed, 2);
assert.ok(deleted.hunks[0]?.lines.every((line) => line.kind === "del"));

const written = parseFileChange("Write", JSON.stringify({
  file_path: "new.ts",
  content: "one\ntwo",
}));
assert.ok(written);
assert.deepEqual(written.hunks[0]?.lines.map((line) => line.kind), ["add", "add"]);
assert.equal(written.added, 2);
assert.equal(written.removed, 0);

const notebook = parseFileChange("NotebookEdit", JSON.stringify({
  file_path: "n.ipynb",
  cell_index: 2,
  mode: "replace",
  source: "print(1)",
}));
assert.ok(notebook);
assert.equal(notebook.subtitle, "cell 2 · replace");
assert.deepEqual(notebook.hunks[0]?.lines, [{ kind: "add", text: "print(1)" }]);

assert.deepEqual(
  lineDiff("alpha\nshared\n", "beta\nshared\n").map((line) => line.kind),
  ["del", "add", "ctx", "ctx"],
);

const fromResult = parseFileChange(
  "Edit",
  JSON.stringify({ file_path: "sort.py", old_string: "a", new_string: "b" }),
  JSON.stringify({
    ok: true,
    path: "sort.py",
    hunks: [{
      oldStart: 4,
      oldCount: 5,
      newStart: 4,
      newCount: 5,
      lines: [
        { kind: "ctx", text: "keep", oldLine: 4, newLine: 4 },
        { kind: "del", text: "a", oldLine: 5 },
        { kind: "add", text: "b", newLine: 5 },
        { kind: "ctx", text: "after", oldLine: 6, newLine: 6 },
      ],
    }],
  }),
);
assert.ok(fromResult);
assert.equal(fromResult.hunks[0]?.oldStart, 4);
assert.equal(fromResult.hunks[0]?.lines[1]?.oldLine, 5);
assert.equal(fromResult.hunks[0]?.lines[2]?.newLine, 5);

console.log("file change preview tests passed");
