import type { McpCapabilities, McpServerConfig, McpToolInfo } from "../protocol/index.js";
import type { RuntimeCatalogItem, RuntimeSummary } from "../terminal-page/types.js";

export function catalogItemsFromMcpServers(servers: McpServerConfig[]): RuntimeCatalogItem[] {
  return servers.map((server) => {
    const item: RuntimeCatalogItem = {
      id: server.id,
      name: (server.name || server.id).trim() || server.id,
      enabled: server.enabled !== false,
    };
    if (server.status) item.status = server.status;
    if (typeof server.toolCount === "number") item.toolCount = server.toolCount;
    if (server.description) item.description = server.description;
    if (server.error) item.error = server.error;
    return item;
  });
}

export function applyMcpServersToRuntime(
  runtime: RuntimeSummary,
  servers: McpServerConfig[],
  tools?: McpToolInfo[],
): RuntimeSummary {
  const mcpServers = catalogItemsFromMcpServers(servers);
  const enabledCount = mcpServers.filter((item) => item.enabled).length;
  return {
    ...runtime,
    mcpServers,
    mcpTools: tools ?? runtime.mcpTools,
    mcpCount: runtime.selectedMcpIds.length || enabledCount,
  };
}

export function toolsFromCapabilities(payload: McpCapabilities): McpToolInfo[] {
  return (payload.tools ?? []).map((tool) => ({
    serverId: String(tool.serverId || tool.server_id || ""),
    name: tool.name,
    description: String(tool.description || "").trim(),
  })).filter((tool) => tool.serverId && tool.name);
}

/** PUT 只提交配置字段；*** 掩码由 Access Layer 用旧值还原。 */
export function sanitizeMcpServersForSave(servers: McpServerConfig[]): McpServerConfig[] {
  return servers.map((server) => {
    const type = server.type === "http" ? "http" : "stdio";
    const next: McpServerConfig = {
      id: server.id,
      description: server.description ?? "",
      type,
      args: type === "stdio" ? server.args ?? [] : [],
      env: type === "stdio" ? server.env ?? {} : {},
      envPassthrough: type === "stdio" ? server.envPassthrough ?? [] : [],
      headers: type === "http" ? server.headers ?? {} : {},
      envHeaders: type === "http" ? server.envHeaders ?? {} : {},
      enabled: server.enabled !== false,
    };
    if (server.name) next.name = server.name;
    if (type === "stdio" && server.command) next.command = server.command;
    if (type === "stdio" && server.cwd) next.cwd = server.cwd;
    if (type === "http" && server.url) next.url = server.url;
    if (type === "http" && server.bearerTokenEnv) next.bearerTokenEnv = server.bearerTokenEnv;
    return next;
  });
}

export type McpSlashAction =
  | { kind: "menu" }
  | { kind: "reload" }
  | { kind: "enable"; target: string }
  | { kind: "disable"; target: string }
  | { kind: "unknown"; detail: string };

export function parseMcpSlashArgs(args: string): McpSlashAction {
  const parts = args.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return { kind: "menu" };
  const verb = parts[0]!.toLowerCase();
  if (verb === "reload") return { kind: "reload" };
  if (verb === "enable" || verb === "disable") {
    return { kind: verb, target: parts.slice(1).join(" ") || "all" };
  }
  return { kind: "unknown", detail: args.trim() };
}

export function setMcpEnabled(
  servers: McpServerConfig[],
  target: string,
  enabled: boolean,
): { servers: McpServerConfig[]; changed: McpServerConfig[]; missing: boolean } {
  const needle = target.trim().toLowerCase();
  if (!needle || needle === "all") {
    const changed = servers.filter((item) => item.enabled !== enabled);
    return {
      servers: servers.map((item) => (item.enabled === enabled ? item : { ...item, enabled })),
      changed,
      missing: false,
    };
  }
  const match = servers.find((item) => item.id.toLowerCase() === needle || (item.name || "").toLowerCase() === needle);
  if (!match) return { servers, changed: [], missing: true };
  if (match.enabled === enabled) return { servers, changed: [], missing: false };
  return {
    servers: servers.map((item) => (item.id === match.id ? { ...item, enabled } : item)),
    changed: [{ ...match, enabled }],
    missing: false,
  };
}

export function formatMcpListLines(servers: McpServerConfig[]): string[] {
  if (servers.length === 0) return ["当前没有配置 MCP。"];
  return servers.map((server) => {
    const name = server.name && server.name !== server.id ? `${server.name} (${server.id})` : server.id;
    const flag = server.enabled === false ? "off" : "on";
    const status = server.status || (server.enabled === false ? "disabled" : "unknown");
    const tools = typeof server.toolCount === "number" ? ` tools=${server.toolCount}` : "";
    return `${flag.padEnd(3)} ${status.padEnd(10)} ${name}${tools}`;
  });
}
