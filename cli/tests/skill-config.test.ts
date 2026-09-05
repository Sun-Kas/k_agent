import assert from "node:assert/strict";
import test from "node:test";
import {
  formatSkillListLines,
  parseSkillSlashArgs,
  sanitizeSkillsForSave,
  setSkillEnabled,
} from "../src/application/skill-config.js";
import type { SkillConfigItem } from "../src/protocol/index.js";

const skills: SkillConfigItem[] = [
  { id: "writer", name: "Writer", description: "d", instructions: "body", enabled: true },
  { id: "hidden", name: "hidden", enabled: false },
];

test("/skill 参数解析 enable/disable，不接受 reload", () => {
  assert.deepEqual(parseSkillSlashArgs(""), { kind: "menu" });
  assert.deepEqual(parseSkillSlashArgs("enable writer"), { kind: "enable", target: "writer" });
  assert.equal(parseSkillSlashArgs("reload").kind, "unknown");
});

test("按 id 或 all 切换 Skill enabled", () => {
  const one = setSkillEnabled(skills, "writer", false);
  assert.equal(one.changed[0]?.enabled, false);
  assert.equal(setSkillEnabled(skills, "missing", true).missing, true);
});

test("保存载荷保留正文并补齐空字段", () => {
  const saved = sanitizeSkillsForSave(skills);
  assert.equal(saved[0]?.instructions, "body");
  assert.equal(saved[1]?.description, "");
  assert.equal(saved[1]?.instructions, "");
});

test("列表行包含开关", () => {
  const text = formatSkillListLines(skills).join("\n");
  assert.match(text, /on +Writer/);
  assert.match(text, /off +hidden/);
});
