import type { SkillConfigItem } from "../protocol/index.js";
import type { RuntimeCatalogItem, RuntimeSummary } from "../terminal-page/types.js";
import { parseMcpSlashArgs, type McpSlashAction } from "./mcp-config.js";

export function parseSkillSlashArgs(args: string): McpSlashAction {
  const parsed = parseMcpSlashArgs(args);
  if (parsed.kind === "reload") return { kind: "unknown", detail: args.trim() };
  return parsed;
}

export function catalogItemsFromSkills(skills: SkillConfigItem[]): RuntimeCatalogItem[] {
  return skills.map((skill) => {
    const item: RuntimeCatalogItem = {
      id: skill.id,
      name: (skill.name || skill.id).trim() || skill.id,
      enabled: skill.enabled !== false,
    };
    if (skill.description) item.description = skill.description;
    return item;
  });
}

export function applySkillsToRuntime(runtime: RuntimeSummary, skills: SkillConfigItem[]): RuntimeSummary {
  const items = catalogItemsFromSkills(skills);
  const enabledCount = items.filter((item) => item.enabled).length;
  return {
    ...runtime,
    skills: items,
    skillCount: runtime.selectedSkillIds.length || enabledCount,
  };
}

export function sanitizeSkillsForSave(skills: SkillConfigItem[]): SkillConfigItem[] {
  return skills.map((skill) => ({
    id: skill.id,
    name: (skill.name || skill.id).trim() || skill.id,
    description: skill.description ?? "",
    instructions: skill.instructions ?? "",
    enabled: skill.enabled !== false,
  }));
}

export function setSkillEnabled(
  skills: SkillConfigItem[],
  target: string,
  enabled: boolean,
): { skills: SkillConfigItem[]; changed: SkillConfigItem[]; missing: boolean } {
  const needle = target.trim().toLowerCase();
  if (!needle || needle === "all") {
    const changed = skills.filter((item) => item.enabled !== enabled);
    return {
      skills: skills.map((item) => (item.enabled === enabled ? item : { ...item, enabled })),
      changed,
      missing: false,
    };
  }
  const match = skills.find((item) => item.id.toLowerCase() === needle || (item.name || "").toLowerCase() === needle);
  if (!match) return { skills, changed: [], missing: true };
  if (match.enabled === enabled) return { skills, changed: [], missing: false };
  return {
    skills: skills.map((item) => (item.id === match.id ? { ...item, enabled } : item)),
    changed: [{ ...match, enabled }],
    missing: false,
  };
}

export function formatSkillListLines(skills: SkillConfigItem[]): string[] {
  if (skills.length === 0) return ["当前没有配置 Skill。"];
  return skills.map((skill) => {
    const name = skill.name && skill.name !== skill.id ? `${skill.name} (${skill.id})` : skill.id;
    const flag = skill.enabled === false ? "off" : "on";
    return `${flag.padEnd(3)} ${name}`;
  });
}
