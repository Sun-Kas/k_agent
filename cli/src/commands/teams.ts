import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { configFromCommand, printJson } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

export async function teamList(command: Command): Promise<void> {
  await withClient(command, async (client) => printJson(await client.listTeams()));
}

export async function teamOpen(teamId: string, command: Command): Promise<void> {
  await withClient(command, async (client) => printJson(await client.getTeam(teamId)));
}

export async function teamCommand(teamId: string, action: "pause" | "resume" | "cancel", command: Command): Promise<void> {
  await withClient(command, async (client) => printJson(await client.commandTeam(teamId, action)));
}

async function withClient(command: Command, operation: (client: AccessLayerClient) => Promise<void>): Promise<void> {
  try { await operation(new AccessLayerClient(configFromCommand(command).endpoint)); }
  catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = EXIT_CODES.connectionError;
  }
}
