/**
 * 多 Agent 团队领域类型：快照、任务状态机、有序事件（seq）与创建草稿。
 */
import type { AgentKind, PermissionMode } from "../types";

export type TeamStatus = "draft" | "running" | "paused" | "completed" | "failed" | "cancelled";
/** 任务生命周期：pending → ready → claimed → running → submitted → completed（或 failed/cancelled）。 */
export type TeamTaskStatus = "pending" | "ready" | "claimed" | "running" | "submitted" | "completed" | "failed" | "cancelled";

export interface TeamSummary {
  id: string;
  name: string;
  goal: string;
  mode: "auto" | "manual";
  status: TeamStatus;
  workspaceDir: string;
  permissionMode: PermissionMode;
  agentCount: number;
  taskCount: number;
  completedTaskCount: number;
  createdAt: string;
  updatedAt: string;
}

export interface TeamAgent {
  id: string;
  teamId: string;
  name: string;
  role: string;
  agentKind: AgentKind;
  modelId?: string | null;
  reasoningEffort?: string | null;
  networkAccess?: boolean | null;
  responsibility: string;
  status: "spawning" | "idle" | "busy" | "waiting" | "paused" | "failed" | "stopped";
  isSupervisor: boolean;
  creationReason: string;
  capabilities: { mcpServerIds: string[]; skillIds: string[] };
  workspaceDir: string;
  permissionMode: PermissionMode;
  updatedAt: string;
}

export interface TeamTask {
  id: string;
  teamId: string;
  title: string;
  description: string;
  taskType: string;
  reviewRequired: boolean;
  status: TeamTaskStatus;
  priority: number;
  ownerAgentId?: string | null;
  dependsOn: string[];
  attempt: number;
  maxAttempts: number;
  leaseUntil?: string | null;
  runId?: string | null;
  error?: string | null;
  updatedAt: string;
}

export interface TeamArtifact {
  id: string;
  teamId: string;
  taskId: string;
  agentId?: string | null;
  kind: string;
  title: string;
  content: string;
  sha256: string;
  version: number;
  status: string;
  workspacePath?: string | null;
  createdAt: string;
  uri: string;
}

export interface TeamMail {
  id: string;
  senderId: string;
  recipientId: string;
  messageType: string;
  content: string;
  artifactIds: string[];
  status: string;
  createdAt: string;
}

/** 团队当前全量视图；lastEventSeq 用于 EventSource afterSeq 续传。 */
export interface TeamSnapshot {
  id: string;
  name: string;
  goal: string;
  mode: "auto" | "manual";
  status: TeamStatus;
  maxParallel: number;
  supervisorAgentId: string;
  workspaceDir: string;
  createdAt: string;
  updatedAt: string;
  lastEventSeq: number;
  supervisorState?: {
    id: string;
    triggerType: string;
    status: "pending" | "running" | "completed" | "failed" | "cancelled";
    attempt: number;
    maxAttempts: number;
    runId?: string | null;
    error?: string | null;
    updatedAt: string;
  } | null;
  agents: TeamAgent[];
  tasks: TeamTask[];
  artifacts: TeamArtifact[];
  mailbox: TeamMail[];
}

/** 有序团队事件；UI 按 seq 去重合并后驱动任务/对话面板。 */
export interface TeamEvent {
  teamId: string;
  seq: number;
  eventId: string;
  type: string;
  payload: Record<string, unknown>;
  occurredAt: string;
}

export interface TeamAgentDraft {
  name: string;
  role: string;
  agentKind: AgentKind;
  modelId?: string;
  reasoningEffort?: string;
  networkAccess?: boolean;
  responsibility: string;
  isSupervisor: boolean;
  mcpServerIds: string[];
  skillIds: string[];
}
