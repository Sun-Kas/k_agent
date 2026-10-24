import assert from "node:assert/strict";
import test from "node:test";
import { rewriteFrameAfterTranscriptWipe, shouldResetTranscriptScrollback, transcriptOwner } from "../src/terminal-page/transcript-scrollback.js";

test("只有离开当前对话或换到另一个会话才清滚动区", () => {
  assert.equal(transcriptOwner("chat", undefined), "draft");
  assert.equal(transcriptOwner("chat", "s1"), "s1");
  assert.equal(transcriptOwner("home", "s1"), "away");
  assert.equal(transcriptOwner("projects", "s1"), "away");
  assert.equal(shouldResetTranscriptScrollback(undefined, "s1"), false);
  assert.equal(shouldResetTranscriptScrollback("s1", "s1"), false);
  assert.equal(shouldResetTranscriptScrollback("draft", "s1"), false);
  assert.equal(shouldResetTranscriptScrollback("s1", "away"), true);
  assert.equal(shouldResetTranscriptScrollback("s1", "s2"), true);
  assert.equal(shouldResetTranscriptScrollback("draft", "away"), true);
  assert.equal(shouldResetTranscriptScrollback("away", "s2"), true);
});

test("清滚动区时用整屏清除替换 Ink 的动态帧擦除", () => {
  const erase = "\u001b[2K\u001b[1A\u001b[2K\u001b[G";
  const chunk = `\u001b[?25l\u001b[1B\u001b[1G${erase}项目页\n\u001b[1A\u001b[5G\u001b[?25h`;
  const rewritten = rewriteFrameAfterTranscriptWipe(chunk);
  assert.equal(rewritten?.consumed, true);
  assert.equal(rewritten?.chunk, "\u001b[2J\u001b[3J\u001b[H项目页\n\u001b[1A\u001b[5G\u001b[?25h");
  assert.equal(rewriteFrameAfterTranscriptWipe("frame\n"), undefined);
  const already = rewriteFrameAfterTranscriptWipe("\u001b[2J\u001b[3J\u001b[H项目页\n");
  assert.equal(already?.consumed, true);
  assert.equal(already?.chunk, "\u001b[2J\u001b[3J\u001b[H项目页\n");
});
