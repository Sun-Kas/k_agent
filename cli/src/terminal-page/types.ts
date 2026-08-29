import type { TimelineItem, TimelineState } from "../application/event-projector.js";
import type {
  ApprovalActivity,
  HealthState,
  ScheduledTaskSummary,
  SessionSummary,
  SessionWorkspaceFileContent,
  SessionWorkspaceListing,
  TeamSummary,
  UserQuestionAnswers,
} from "../protocol/index.js";

export type TerminalSurface = "home" | "chat" | "team" | "automation" | "doctor";
export type TerminalFocus = "session-rail" | "timeline" | "inspector" | "composer" | "overlay";

export interface RuntimeSummary {
  endpoint: string;
  agentKind: string;
  modelId: string;
  reasoningEffort: string;
  permissionMode: string;
  mcpCount: number;
  skillCount: number;
}

export interface TerminalPageViewModel {
  surface: TerminalSurface;
  connected: boolean;
  loading: boolean;
  health: HealthState | undefined;
  runtime: RuntimeSummary;
  sessions: SessionSummary[];
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
  | { type: "open_surface"; surface: TerminalSurface }
  | { type: "select_activity"; activityId: string }
  | { type: "open_workspace"; path?: string }
  | { type: "stop_run" }
  | { type: "cancel_run" }
  | { type: "answer_interrupt"; interruptId: string; payload: InterruptAnswer }
  | { type: "retry_connection" }
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
