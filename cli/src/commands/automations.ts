import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { configFromCommand, printJson } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

export async function automationList(command: Command): Promise<void> {
  await withClient(command, async (client) => printJson(await client.listScheduledTasks()));
}

export async function automationShow(taskId: string, command: Command): Promise<void> {
  await withClient(command, async (client) => printJson(await client.getScheduledTask(taskId)));
}

export async function automationAction(taskId: string, action: "pause" | "resume" | "run-now", command: Command): Promise<void> {
  await withClient(command, async (client) => printJson(await client.scheduledTaskAction(taskId, action)));
}

export async function automationHistory(taskId: string, command: Command): Promise<void> {
  await withClient(command, async (client) => printJson(await client.listScheduledRuns(taskId)));
}

async function withClient(command: Command, operation: (client: AccessLayerClient) => Promise<void>): Promise<void> {
  try { await operation(new AccessLayerClient(configFromCommand(command).endpoint)); }
  catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = EXIT_CODES.connectionError;
  }
}
