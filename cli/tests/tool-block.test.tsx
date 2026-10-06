import React from "react";
import assert from "node:assert/strict";
import test from "node:test";
import { render } from "ink-testing-library";
import { ToolBlock } from "../src/terminal-page/renderers/ToolBlock.js";

test("Edit 工具块显示路径统计和着色 hunk，而不是原始 JSON", () => {
  const view = render(
    <ToolBlock
      expanded={false}
      item={{
        id: "call-1",
        sequence: 1,
        kind: "tool",
        name: "Edit",
        arguments: JSON.stringify({ file_path: "sort.py", old_string: "a", new_string: "b" }),
        result: JSON.stringify({
          ok: true,
          path: "sort.py",
          hunks: [{
            oldStart: 5,
            oldCount: 2,
            newStart: 5,
            newCount: 2,
            lines: [
              { kind: "del", text: "a", oldLine: 5 },
              { kind: "add", text: "b", newLine: 5 },
            ],
          }],
        }),
        status: "complete",
      }}
    />,
  );
  const frame = view.lastFrame() ?? "";
  assert.match(frame, /Edit/);
  assert.match(frame, /sort\.py/);
  assert.match(frame, /\+1/);
  assert.match(frame, /5 - a/);
  assert.match(frame, /5 \+ b/);
  assert.doesNotMatch(frame, /old_string/);
  assert.doesNotMatch(frame, /@@ /);
  view.unmount();
});
