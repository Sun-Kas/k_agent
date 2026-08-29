import type { Command } from "commander";
import { resolveCliConfig, type CliRuntimeConfig } from "../config/index.js";
import type { CliSessionMode, PermissionMode, ReasoningEffort } from "../protocol/index.js";

export interface GlobalCommandOptions {
  endpoint?: string;
  agent?: string;
  model?: string;
  reasoning?: ReasoningEffort;
  permission?: PermissionMode;
  cliSessionMode?: CliSessionMode;
  mcp?: string[];
  skill?: string[];
  plain?: boolean;
}

export function configFromCommand(command: Command): CliRuntimeConfig {
  const options = command.optsWithGlobals<GlobalCommandOptions>();
  return resolveCliConfig({
    ...(options.endpoint ? { endpoint: options.endpoint } : {}),
    ...(options.agent ? { agentKind: options.agent } : {}),
    ...(options.model ? { modelId: options.model } : {}),
    ...(options.reasoning ? { reasoningEffort: options.reasoning } : {}),
    ...(options.permission ? { permissionMode: options.permission } : {}),
    ...(options.cliSessionMode ? { cliSessionMode: options.cliSessionMode } : {}),
    ...(options.mcp ? { mcpServerIds: options.mcp } : {}),
    ...(options.skill ? { skillIds: options.skill } : {}),
    ...(options.plain !== undefined ? { plain: options.plain } : {}),
  });
}

export function printJson(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}
