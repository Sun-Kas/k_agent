import { emptyTimeline } from "./event-projector.js";
import type { CliRuntimeConfig } from "../config/index.js";
import type { TerminalPageViewModel } from "../terminal-page/index.js";

export function emptyViewModel(config: CliRuntimeConfig): TerminalPageViewModel {
  return {
    surface: "home",
    connected: false,
    loading: true,
    health: undefined,
    runtime: {
      endpoint: config.endpoint,
      agentKind: config.agentKind,
      modelId: config.modelId ?? "",
      reasoningEffort: config.reasoningEffort,
      permissionMode: config.permissionMode,
      mcpCount: config.mcpServerIds.length,
      skillCount: config.skillIds.length,
    },
    sessions: [],
    activeSessionId: undefined,
    activeSessionTitle: undefined,
    timeline: emptyTimeline(),
    selectedActivityId: undefined,
    workspace: undefined,
    workspaceFile: undefined,
    teams: [],
    automations: [],
    doctorLines: [],
    notice: undefined,
    error: undefined,
  };
}
