import type { CliRuntimeConfig } from "../config/index.js";
import type { DetectedAgent, ModelProfile, PermissionMode } from "../protocol/index.js";
import type { RuntimeChoice, RuntimeSummary } from "../terminal-page/types.js";

export const PERMISSION_CHOICES: RuntimeChoice[] = [
  { id: "default", name: "default", enabled: true, note: "沙箱与工具审批" },
  { id: "full_access", name: "full_access", enabled: true, note: "跳过沙箱与审批" },
];

export type MenuOrSetAction =
  | { kind: "menu" }
  | { kind: "set"; target: string }
  | { kind: "unknown"; detail: string };

export function parseMenuOrSetArgs(args: string): MenuOrSetAction {
  const trimmed = args.trim();
  if (!trimmed) return { kind: "menu" };
  const parts = trimmed.split(/\s+/).filter(Boolean);
  const verb = parts[0]!.toLowerCase();
  if (verb === "set" || verb === "use") {
    const target = parts.slice(1).join(" ");
    if (!target) return { kind: "unknown", detail: trimmed };
    return { kind: "set", target };
  }
  return { kind: "set", target: trimmed };
}

export function choicesFromModels(models: ModelProfile[]): RuntimeChoice[] {
  return models.map((item) => {
    const choice: RuntimeChoice = {
      id: item.id,
      name: item.name || item.id,
      enabled: item.enabled,
    };
    if (!item.enabled) choice.note = "未启用";
    else if (!item.apiKeyConfigured) choice.note = "未配置密钥";
    else if (item.model) choice.note = item.model;
    return choice;
  });
}

export function choicesFromAgents(agents: DetectedAgent[]): RuntimeChoice[] {
  return agents.map((item) => {
    const choice: RuntimeChoice = {
      id: item.kind,
      name: item.name || item.kind,
      enabled: item.available,
    };
    if (!item.available) choice.note = item.detail?.trim() || "当前不可用";
    else if (item.version) choice.note = item.version;
    return choice;
  });
}

export function findChoice(items: RuntimeChoice[], target: string): RuntimeChoice | undefined {
  const needle = target.trim().toLowerCase();
  if (!needle) return undefined;
  return items.find((item) => item.id.toLowerCase() === needle || item.name.toLowerCase() === needle);
}

export function applyRuntimeToSummary(runtime: RuntimeSummary, config: CliRuntimeConfig): RuntimeSummary {
  return {
    ...runtime,
    agentKind: config.agentKind,
    modelId: config.modelId ?? "",
    permissionMode: config.permissionMode,
    selectedMcpIds: [...config.mcpServerIds],
    selectedSkillIds: [...config.skillIds],
  };
}

export function selectModel(
  config: CliRuntimeConfig,
  models: RuntimeChoice[],
  target: string,
): { config: CliRuntimeConfig; notice: string } | { error: string } {
  const match = findChoice(models, target);
  if (!match) return { error: `未找到模型 “${target}”` };
  if (!match.enabled) return { error: `模型 “${match.id}” 未启用，请先在配置中心打开` };
  return {
    config: { ...config, modelId: match.id },
    notice: `已切换模型 ${match.name === match.id ? match.id : `${match.name} (${match.id})`}，下一轮生效`,
  };
}

export function selectAgent(
  config: CliRuntimeConfig,
  agents: RuntimeChoice[],
  target: string,
): { config: CliRuntimeConfig; notice: string } | { error: string } {
  const match = findChoice(agents, target);
  if (!match) return { error: `未找到 Agent “${target}”` };
  if (!match.enabled) return { error: `Agent “${match.id}” 当前不可用` };
  return {
    config: { ...config, agentKind: match.id },
    notice: `已切换 Agent ${match.name === match.id ? match.id : `${match.name} (${match.id})`}，下一轮生效`,
  };
}

export function selectPermission(
  config: CliRuntimeConfig,
  target: string,
): { config: CliRuntimeConfig; notice: string } | { error: string } {
  const match = findChoice(PERMISSION_CHOICES, target);
  if (!match) return { error: "权限模式只支持 default 或 full_access" };
  return {
    config: { ...config, permissionMode: match.id as PermissionMode },
    notice: `已切换权限 ${match.id}，下一轮生效`,
  };
}
