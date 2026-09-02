import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { choicesFromAgents } from "../application/runtime-switch.js";
import { configFromCommand } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

export async function agentList(command: Command): Promise<void> {
  const client = new AccessLayerClient(configFromCommand(command).endpoint);
  try {
    const catalog = await client.agents();
    const agents = choicesFromAgents(catalog.agents);
    if (agents.length === 0) {
      process.stdout.write("当前没有可用 Agent。\n");
      return;
    }
    for (const agent of agents) {
      const flag = agent.enabled ? "on " : "off";
      const name = agent.name === agent.id ? agent.id : `${agent.name} (${agent.id})`;
      const note = agent.note ? ` · ${agent.note}` : "";
      process.stdout.write(`${flag} ${name}${note}\n`);
    }
  } catch (error) {
    fail(error);
  }
}

function fail(error: unknown): void {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = EXIT_CODES.connectionError;
}
