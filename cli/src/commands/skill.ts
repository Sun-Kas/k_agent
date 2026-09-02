import type { Command } from "commander";
import {
  formatSkillListLines,
  sanitizeSkillsForSave,
  setSkillEnabled,
} from "../application/skill-config.js";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { configFromCommand } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

export async function skillList(command: Command): Promise<void> {
  const client = clientFrom(command);
  try {
    const payload = await client.getSkillsConfig();
    process.stdout.write(`${formatSkillListLines(payload.skills).join("\n")}\n`);
  } catch (error) {
    fail(error);
  }
}

export async function skillEnable(target: string, command: Command): Promise<void> {
  await mutateEnabled(command, target, true);
}

export async function skillDisable(target: string, command: Command): Promise<void> {
  await mutateEnabled(command, target, false);
}

async function mutateEnabled(command: Command, target: string, enabled: boolean): Promise<void> {
  const client = clientFrom(command);
  try {
    const payload = await client.getSkillsConfig();
    const next = setSkillEnabled(payload.skills, target, enabled);
    if (next.missing) {
      process.stderr.write(`未找到 Skill “${target}”\n`);
      process.exitCode = EXIT_CODES.configError;
      return;
    }
    if (next.changed.length === 0) {
      process.stdout.write(`Skill 已是 ${enabled ? "启用" : "关闭"} 状态\n`);
      return;
    }
    await client.saveSkillsConfig(sanitizeSkillsForSave(next.skills));
    const after = await client.getSkillsConfig();
    process.stdout.write(`${formatSkillListLines(after.skills).join("\n")}\n`);
  } catch (error) {
    fail(error);
  }
}

function clientFrom(command: Command): AccessLayerClient {
  return new AccessLayerClient(configFromCommand(command).endpoint);
}

function fail(error: unknown): void {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = EXIT_CODES.connectionError;
}
