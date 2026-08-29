import type { AgentKind, CliSessionMode, PermissionMode, ReasoningEffort } from "../protocol/index.js";

export interface CliRuntimeConfig {
  endpoint: string;
  agentKind: AgentKind;
  modelId?: string;
  reasoningEffort: ReasoningEffort;
  permissionMode: PermissionMode;
  cliSessionMode: CliSessionMode;
  mcpServerIds: string[];
  skillIds: string[];
  plain: boolean;
}

export interface CliConfigOverrides extends Partial<CliRuntimeConfig> {
  endpoint?: string;
}

/**
 * CLI 只读取自己的环境变量和参数，不读取服务端 .env 或 ~/.k_agent。
 * 服务端凭据与持久状态始终由 Access Layer 管理。
 */
export function resolveCliConfig(overrides: CliConfigOverrides = {}): CliRuntimeConfig {
  const endpoint = normalizeEndpoint(
    overrides.endpoint
      ?? process.env.K_AGENT_ENDPOINT
      ?? "http://127.0.0.1:3001",
  );
  const modelId = overrides.modelId ?? process.env.K_AGENT_MODEL;
  return {
    endpoint,
    agentKind: overrides.agentKind ?? process.env.K_AGENT_AGENT ?? "k_agent",
    ...(modelId ? { modelId } : {}),
    reasoningEffort: overrides.reasoningEffort ?? "none",
    permissionMode: overrides.permissionMode ?? "default",
    cliSessionMode: overrides.cliSessionMode ?? "ephemeral",
    mcpServerIds: overrides.mcpServerIds ?? [],
    skillIds: overrides.skillIds ?? [],
    plain: overrides.plain ?? process.env.NO_COLOR !== undefined,
  };
}

export function normalizeEndpoint(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  const parsed = new URL(trimmed);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("K Agent endpoint 只支持 http 或 https");
  }
  return parsed.toString().replace(/\/$/, "");
}
