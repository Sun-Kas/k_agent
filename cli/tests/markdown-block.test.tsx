import React from "react";
import assert from "node:assert/strict";
import test from "node:test";
import { render } from "ink-testing-library";
import { MarkdownBlock } from "../src/terminal-page/renderers/MarkdownBlock.js";

test("MarkdownBlock 渲染粗体、行内代码和列表而不留下标记", () => {
  const view = render(
    <MarkdownBlock
      content={
        "我是 **K Agent**，可以维护 `access_layer/` 与 `backend/`。\n\n- 读写文件\n- 搜索网页"
      }
    />,
  );
  const frame = view.lastFrame() ?? "";
  assert.match(frame, /K Agent/);
  assert.match(frame, /access_layer\//);
  assert.match(frame, /读写文件/);
  assert.doesNotMatch(frame, /\*\*K Agent\*\*/);
  assert.doesNotMatch(frame, /`access_layer\/`/);
  assert.doesNotMatch(frame, /^- 读写文件/m);
  view.unmount();
});

test("MarkdownBlock 渲染围栏代码块语言和行号", () => {
  const view = render(<MarkdownBlock content={"```ts\nconst n = 1;\n```"} />);
  const frame = view.lastFrame() ?? "";
  assert.match(frame, /ts/);
  assert.match(frame, /const n = 1;/);
  assert.match(frame, /1 /);
  assert.doesNotMatch(frame, /```/);
  view.unmount();
});
