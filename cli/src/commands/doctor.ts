import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { configFromCommand } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

export async function doctor(command: Command): Promise<void> {
  const config = configFromCommand(command);
  const client = new AccessLayerClient(config.endpoint);
  const lines = [
    `Node.js              ${process.version}`,
    `TTY                  stdin=${Boolean(process.stdin.isTTY)} stdout=${Boolean(process.stdout.isTTY)}`,
    `Columns              ${process.stdout.columns ?? "unknown"}`,
    `Access Layer         ${config.endpoint}`,
  ];
  try {
    const [health, catalog, agents, models] = await Promise.all([
      client.health(), client.catalog(), client.agents(), client.models(),
    ]);
    lines.push(
      `Access Layer health  ${health.ok ? "ok" : "degraded"}`,
      `Agent Backend        ${health.agentBackendOk ? "ok" : "unavailable"}`,
      `Tools                local=${health.localToolCount} mcp=${health.mcpToolCount}`,
      `Catalog              mcp=${catalog.mcpServers.length} skills=${catalog.skills.length}`,
      `Agents               ${agents.agents.filter((item) => item.available).map((item) => item.kind).join(", ") || "none"}`,
      `Models               ${models.filter((item) => item.enabled).length} enabled`,
    );
    if (health.bashSandbox) lines.push(`Bash sandbox          ${health.bashSandbox.available ? health.bashSandbox.mode : health.bashSandbox.reason}`);
    process.stdout.write(`${lines.join("\n")}\n`);
    if (!health.ok || !health.agentBackendOk) process.exitCode = EXIT_CODES.connectionError;
  } catch (error) {
    lines.push(`Result               failed: ${error instanceof Error ? error.message : String(error)}`);
    process.stdout.write(`${lines.join("\n")}\n`);
    process.exitCode = EXIT_CODES.connectionError;
  }
}
