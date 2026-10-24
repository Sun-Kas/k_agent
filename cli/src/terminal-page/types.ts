import type { TimelineItem, TimelineState } from "../application/event-projector.js";
import type {
  ApprovalActivity,
  DirectoryListing,
  HealthState,
  McpToolInfo,
  ProjectSummary,
  ScheduledTaskSummary,
  SessionSummary,
  SessionWorkspaceFileContent,
  SessionWorkspaceListing,
  TeamSummary,
  UserQuestionAnswers,
} from "../protocol/index.js";

export type TerminalSurface = "home" | "chat" | "team" | "automation" | "doctor" | "projects" | "skills" | "connectors";
export type TerminalFocus = "session-rail" | "timeline" | "inspector" | "composer" | "overlay";

export interface RuntimeCatalogItem {
  id: string;
  name: string;
  enabled: boolean;
  status?: string;
  toolCount?: number;
  description?: string;
  error?: string;
}

export interface RuntimeChoice {
  id: string;
  name: string;
  enabled: boolean;
  note?: string;
  models?: RuntimeChoice[];
  defaultModelId?: string;
}

export interface RuntimeSummary {
  endpoint: string;
  agentKind: string;
  modelId: string;
  reasoningEffort: string;
  permissionMode: string;
  mcpCount: number;
  skillCount: number;
  /** Catalog snapshot for read-only `/mcp` / `/skill` listings. */
  mcpServers: RuntimeCatalogItem[];
  skills: RuntimeCatalogItem[];
  /** 本轮精确选择的 ID；空数组表示不选，不能解释为全部启用。 */
  selectedMcpIds: string[];
  selectedSkillIds: string[];
  mcpTools: McpToolInfo[];
  models: RuntimeChoice[];
  modelCatalog?: RuntimeChoice[];
  agents: RuntimeChoice[];
}

export interface TerminalPageViewModel {
  surface: TerminalSurface;
  connected: boolean;
  loading: boolean;
  health: HealthState | undefined;
  runtime: RuntimeSummary;
  sessions: SessionSummary[];
  projects: ProjectSummary[];
  pinnedProjectIds: string[];
  activeProjectId?: string | undefined;
  directory?: DirectoryListing | undefined;
  projectBranches?: { projectId: string; branches: string[] } | undefined;
  marketplace?: {
    kind: "skill" | "mcp";
    loading: boolean;
    items: Array<{ sourceId: string; title: string; summary: string; installed: boolean }>;
    notice?: string;
  } | undefined;
  activeSessionId: string | undefined;
  activeSessionTitle: string | undefined;
  timeline: TimelineState;
  selectedActivityId: string | undefined;
  workspace: SessionWorkspaceListing | undefined;
  workspaceFile: SessionWorkspaceFileContent | undefined;
  teams: TeamSummary[];
  automations: ScheduledTaskSummary[];
  doctorLines: string[];
  notice: string | undefined;
  error: string | undefined;
  mcpBusy: boolean;
  workbenchOpen?: boolean;
  preferences?: import("../application/preferences.js").ClientPreferences;
  environment?: import("../protocol/index.js").SessionEnvironment | null | undefined;
  attachments?: import("../protocol/index.js").MediaAttachment[];
  workbench?: {
    operationId: string;
    busy: boolean;
    result?: unknown;
    error?: string;
    formValues?: Record<string, string>;
  };
}

export interface TerminalPageProps {
  /** 页面只读取 application 层投影后的状态，不拥有服务端写权限。 */
  model: TerminalPageViewModel;
  /** 页面只报告用户意图；网络、checkpoint 和 run 生命周期由 application 层处理。 */
  onAction: (action: TerminalPageAction) => void;
}

export type TerminalPageAction =
  | { type: "submit_prompt"; text: string }
  | { type: "slash_command"; command: string; arguments: string }
  | { type: "select_session"; sessionId: string }
  | { type: "new_session" }
  | { type: "enter_project"; projectId: string }
  | { type: "clear_project" }
  | { type: "start_project_session"; projectId: string; environment: "local" | "worktree"; startingState: "default" | "working-tree" }
  | { type: "fork_session"; sessionId: string; environment: "same-directory" | "worktree"; throughMessageId?: string }
  | { type: "bind_session"; sessionId: string; projectId: string }
  | { type: "delete_session"; sessionId: string }
  | { type: "pin_project"; projectId: string }
  | { type: "set_target_branch"; projectId: string; name: string; targetBranch: string | null }
  | { type: "create_project"; rootPath: string }
  | { type: "browse_directory"; path?: string }
  | { type: "make_directory"; parentPath: string; name: string }
  | { type: "load_branches"; projectId: string }
  | { type: "handoff_session"; target: "local" | "worktree" }
  | { type: "compact_session"; instructions: string; reset?: boolean }
  | { type: "open_surface"; surface: TerminalSurface }
  | { type: "select_activity"; activityId: string }
  | { type: "open_workspace"; path?: string }
  | { type: "stop_run" }
  | { type: "cancel_run" }
  | { type: "answer_interrupt"; interruptId: string; payload: InterruptAnswer }
  | { type: "retry_connection" }
  | { type: "refresh_mcp" }
  | { type: "toggle_capability"; kind: "mcp" | "skill"; id: string; selection?: "all" | "none" }
  | { type: "toggle_mcp"; serverId: string }
  | { type: "reload_mcp" }
  | { type: "set_model"; modelId: string }
  | { type: "set_agent"; agentKind: string }
  | { type: "set_permission"; permissionMode: string }
  | { type: "refresh_skills" }
  | { type: "toggle_skill"; skillId: string }
  | { type: "open_workbench" }
  | { type: "close_workbench" }
  | { type: "workbench_execute"; operationId: string; values: Record<string, string> }
  | { type: "workbench_load"; operationId: string; values: Record<string, string> }
  | { type: "quit" };

export type InterruptAnswer =
  | { action: "approve"; scope: "once" | "run" }
  | { action: "deny" }
  | { action: "cancel" }
  | { action: "answer"; answers: UserQuestionAnswers };

export interface OverlayState {
  kind: "none" | "commands" | "sessions" | "help" | "approval" | "question";
  approval?: ApprovalActivity;
}

export function selectedTimelineItem(model: TerminalPageViewModel): TimelineItem | undefined {
  return model.timeline.items.find((item) => item.id === model.selectedActivityId)
    ?? [...model.timeline.items].reverse().find((item) => item.kind !== "user");
}
