import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import {
  formatMcpListLines,
  sanitizeMcpServersForSave,
  setMcpEnabled,
} from "../application/mcp-config.js";
import { configFromCommand } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

export async function mcpList(command: Command): Promise<void> {
  const client = clientFrom(command);
  try {
    const payload = await client.getMcpConfig();
    process.stdout.write(`${formatMcpListLines(payload.servers).join("\n")}\n`);
  } catch (error) {
    fail(error);
  }
}

export async function mcpEnable(target: string, command: Command): Promise<void> {
  await mutateEnabled(command, target, true);
}

export async function mcpDisable(target: string, command: Command): Promise<void> {
  await mutateEnabled(command, target, false);
}

export async function mcpReload(command: Command): Promise<void> {
  const client = clientFrom(command);
  try {
    await client.reloadMcp();
    const payload = await client.getMcpConfig();
    process.stdout.write(`${formatMcpListLines(payload.servers).join("\n")}\n`);
  } catch (error) {
    fail(error);
  }
}

async function mutateEnabled(command: Command, target: string, enabled: boolean): Promise<void> {
  const client = clientFrom(command);
  try {
    const payload = await client.getMcpConfig();
    const next = setMcpEnabled(payload.servers, target, enabled);
    if (next.missing) {
      process.stderr.write(`未找到 MCP “${target}”\n`);
      process.exitCode = EXIT_CODES.configError;
      return;
    }
    if (next.changed.length === 0) {
      process.stdout.write(`MCP 已是 ${enabled ? "启用" : "关闭"} 状态\n`);
      return;
    }
    await client.saveMcpConfig(sanitizeMcpServersForSave(next.servers));
    const after = await client.getMcpConfig();
    process.stdout.write(`${formatMcpListLines(after.servers).join("\n")}\n`);
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
