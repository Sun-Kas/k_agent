import type { AgentKind, ReasoningEffort } from "../types";

export type ScheduleKind = "once" | "daily" | "weekly";
export type ScheduledRunStatus = "queued" | "running" | "succeeded" | "failed" | "missed";

export interface ScheduledRun {
  id: string;
  taskId: string;
  triggerType: "schedule" | "manual";
  scheduledFor: string;
  status: ScheduledRunStatus;
  sessionId?: string | null;
  agentRunId?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
}

export interface ScheduledTaskInput {
  name: string;
  prompt: string;
  scheduleKind: ScheduleKind;
  localDate?: string | null;
  weekdays: number[];
  localTime: string;
  timezone: string;
  agentKind: AgentKind;
  modelId: string;
  reasoningEffort: ReasoningEffort;
  mcpServerIds: string[];
  skillIds: string[];
}

export interface ScheduledTask extends ScheduledTaskInput {
  id: string;
  status: "active" | "paused";
  nextRunAt?: string | null;
  createdAt: string;
  updatedAt: string;
  latestRun?: ScheduledRun | null;
}
