export type ChatRole = "user" | "assistant" | "system" | "tool";
export type AgentKind = "k_agent" | "codex" | "claude_code" | string;
export type CliSessionMode = "ephemeral" | "resume";
export type PermissionMode = "default" | "full_access";
export type ReasoningEffort = "none" | "low" | "medium" | "high" | "max";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  attachments?: MediaAttachment[];
  createdAt: string;
  meta?: {
    toolName?: string;
    runId?: string;
    toolCallId?: string;
  };
}

export interface SessionSummary {
  projectId?: string | null;
  environment?: SessionEnvironment | null;
  id: string;
  title: string;
  updatedAt: string;
  messageCount: number;
}

export interface SessionEnvironment {
  kind: "local" | "worktree";
  cwd: string;
  projectId: string;
  repositoryRoot?: string | null;
  gitCommonDir?: string | null;
  baseOid?: string | null;
  branchRef?: string | null;
  detached: boolean;
  worktreeId?: string | null;
}

/** Project：稳定文件夹 + 会话归属关系，不是新的运行时。 */
export interface ProjectSummary {
  id: string;
  name: string;
  rootPath: string;
  isGitRepository?: boolean;
  repositoryRoot?: string | null;
  gitCommonDir?: string | null;
  targetBranch?: string | null;
  sessionCount: number;
  createdAt: string;
  updatedAt: string;
}

/** 目录浏览弹窗（新建 Project）里一个可进入的子目录条目。 */
export interface DirectoryEntry {
  name: string;
  path: string;
}

export interface ProjectRepository {
  isGitRepository: boolean;
  branchRef?: string | null;
  targetBranch?: string | null;
  branches: string[];
}

export interface IntegrationState {
  status: "idle" | "merging" | "conflicts" | "ready" | "publishing" | "completed" | "aborted" | "failed";
  targetBranch?: string | null;
  conflicts: string[];
  message?: string;
  resultOid?: string;
}

export interface IntegrationFile {
  path: string;
  content: string;
  binary: boolean;
  hash: string;
}

/** `/api/fs/browse` 的返回：当前目录、父目录（到盘符根时为 null）和子目录列表。 */
export interface DirectoryListing {
  path: string;
  parent: string | null;
  entries: DirectoryEntry[];
}

export interface SessionWorkspaceFile {
  path: string;
  name: string;
  size: number;
  modifiedAt: number;
}

export interface SessionWorkspaceListing {
  sessionId: string;
  root: string;
  files: SessionWorkspaceFile[];
}

export interface SessionWorkspaceFileContent {
  sessionId: string;
  path: string;
  name: string;
  content: string;
  truncated: boolean;
  binary: boolean;
  size: number;
}

export interface UserQuestionOption {
  label: string;
  description: string;
}

export interface UserQuestion {
  id: string;
  header: string;
  question: string;
  options: UserQuestionOption[];
  multiSelect: boolean;
}

export type UserQuestionAnswers = Record<string, {
  selected: string[];
  custom: string;
}>;

export interface ApprovalActivity {
  id: string;
  threadId: string;
  runId: string;
  agentKind: AgentKind;
  category: string;
  title: string;
  message: string;
  detail: Record<string, unknown>;
  status: "pending" | "submitting" | "approved" | "answered" | "denied" | "cancelled" | "expired" | "unknown_outcome" | "resume_failed" | "error";
  sequence: number;
  error?: string;
  answers?: UserQuestionAnswers;
}

export interface SessionState {
  environment?: SessionEnvironment | null;
  sessionId: string;
  events: AgUiEvent[];
  openInterrupts?: Array<Omit<ApprovalActivity, "sequence"> & {
    requestHash?: string;
    toolCallId?: string;
  }>;
  capabilities?: {
    mcpServerIds: string[];
    skillIds: string[];
    permissionMode?: PermissionMode;
  } | null;
}

export interface HealthState {
  ok: boolean;
  agentBackendOk: boolean;
  model: string;
  localToolCount: number;
  mcpToolCount: number;
  bashSandbox?: {
    available: boolean;
    mode: string;
    reason: string;
    needsInstall: boolean;
    userSummary?: string;
  } | null;
}

export interface RuntimeOption {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
}

export interface RuntimeCatalog {
  mcpServers: RuntimeOption[];
  skills: RuntimeOption[];
  sources: { mcp: string; skills: string };
}

export interface McpServerConfig {
  id: string;
  name?: string;
  description?: string;
  type?: "stdio" | "http";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  envPassthrough?: string[];
  cwd?: string;
  url?: string;
  bearerTokenEnv?: string;
  headers?: Record<string, string>;
  envHeaders?: Record<string, string>;
  enabled: boolean;
  connected?: boolean;
  status?: string;
  scope?: string;
  transport?: string;
  toolCount?: number;
  resourceCount?: number;
  error?: string | null;
}

export interface McpConfigPayload {
  path?: string;
  source?: string;
  servers: McpServerConfig[];
  warnings?: string[];
}

export interface McpToolInfo {
  serverId: string;
  name: string;
  description: string;
}

export interface McpCapabilities {
  tools: Array<{
    server_id?: string;
    serverId?: string;
    name: string;
    description?: string | null;
  }>;
}

export interface DetectedAgent {
  kind: AgentKind;
  name: string;
  available: boolean;
  command?: string | null;
  version?: string | null;
  detail?: string | null;
  requires_cli?: boolean;
  supports_resume?: boolean;
  default_cli_session_mode?: CliSessionMode;
  supportsModelSwitch?: boolean;
  defaultModelId?: string | null;
  models?: Array<{ id: string; name: string; supportsReasoning?: boolean }>;
}

export interface AgentsCatalog {
  defaultKind: AgentKind;
  agents: DetectedAgent[];
}

export interface ModelProfile {
  id: string;
  name: string;
  model: string;
  baseUrl: string;
  apiKeyConfigured: boolean;
  apiKey?: string;
  apiKeyEnv?: string;
  multimodal?: boolean;
  inputModalities?: Array<"text" | "image" | "video">;
  contextWindow?: number;
  maxOutputTokens?: number;
  contextSafetyTokens?: number;
  autoCompactEnabled?: boolean;
  compactModelId?: string | null;
  supportsReasoning: boolean;
  enabled: boolean;
}

export interface SkillConfigItem {
  id: string;
  name: string;
  description?: string;
  instructions?: string;
  enabled: boolean;
}

export interface SkillsConfigPayload {
  path?: string;
  skillDir?: string;
  skills: SkillConfigItem[];
}

export interface AgUiInterrupt {
  id: string;
  reason: string;
  message: string;
  toolCallId?: string;
  responseSchema?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

/**
 * CLI 只消费 Access Layer 发出的公开 AG-UI 事件。
 * 联合类型保持服务端原始生命周期，页面不得根据内容猜测 start/end。
 */
export type AgUiEvent =
  | { type: "input_message"; runId?: string; message: ChatMessage }
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | { type: "RUN_FINISHED"; threadId: string; runId: string; result?: unknown; outcome?: { type: "interrupt"; interrupts: AgUiInterrupt[] } }
  | { type: "RUN_ERROR"; message: string; code?: string }
  | { type: "STATE_SNAPSHOT"; snapshot: unknown }
  | { type: "MESSAGES_SNAPSHOT"; messages: unknown[] }
  | { type: "TEXT_MESSAGE_START"; messageId: string; role: "assistant" }
  | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "TEXT_MESSAGE_END"; messageId: string }
  | { type: "REASONING_START"; messageId: string }
  | { type: "REASONING_MESSAGE_START"; messageId: string; role: "reasoning"; rawEvent?: unknown }
  | { type: "REASONING_MESSAGE_CONTENT"; messageId: string; delta: string; rawEvent?: unknown }
  | { type: "REASONING_MESSAGE_END"; messageId: string; rawEvent?: unknown }
  | { type: "REASONING_END"; messageId: string }
  | { type: "TOOL_CALL_START"; toolCallId: string; toolCallName: string }
  | { type: "TOOL_CALL_ARGS"; toolCallId: string; delta: string }
  | { type: "TOOL_CALL_END"; toolCallId: string }
  | { type: "TOOL_CALL_RESULT"; messageId: string; toolCallId: string; content: string; role?: "tool" }
  | { type: "ACTIVITY_SNAPSHOT"; messageId: string; activityType: string; content: unknown; replace?: boolean }
  | { type: "CUSTOM"; name: string; value: Record<string, unknown> };

export interface AgUiRunInput {
  threadId: string;
  runId: string;
  state: Record<string, never>;
  messages: Array<Pick<ChatMessage, "id" | "role" | "content">>;
  tools: unknown[];
  context: unknown[];
  resume?: Array<{
    interruptId: string;
    status: "resolved" | "cancelled";
    payload?: { approved: boolean; scope?: "once" | "run"; reconfirm?: boolean }
      | { answers: UserQuestionAnswers; reconfirm?: boolean };
  }>;
  forwardedProps: {
    attachments?: MediaAttachment[];
    modelId?: string;
    mcpServerIds?: string[];
    skillIds?: string[];
    reasoningEffort?: ReasoningEffort;
    agentKind?: AgentKind;
    agentOptions?: {
      cliSessionMode?: CliSessionMode;
      permissionMode?: PermissionMode;
      networkAccess?: boolean;
      resumeSessionId?: string;
      claudeAutoApproveTools?: string[];
    };
  };
}

export interface TeamSummary extends Record<string, unknown> {
  id: string;
  name?: string;
  status?: string;
}

export interface ScheduledTaskSummary extends Record<string, unknown> {
  id: string;
  name: string;
  status: "active" | "paused" | string;
  nextRunAt?: string | null;
}

export interface ScheduledRunSummary extends Record<string, unknown> {
  id: string;
  taskId: string;
  status: string;
  scheduledFor: string;
  sessionId?: string | null;
}

export interface MediaAttachment { name: string; dataUrl: string; type: string }

export interface SessionGitChange {
  path: string;
  status: string;
  originalPath?: string | null;
}

export interface SessionChanges {
  sessionId: string;
  baseOid?: string | null;
  files: SessionGitChange[];
}


export interface SessionContextStatus {
  generation: number;
  boundaryId?: string | null;
  lastCompactedAt?: string | null;
  trigger?: string | null;
  beforeTokens?: number | null;
  afterTokens?: number | null;
  savedTokens?: number | null;
  warning: boolean;
  autoDisabled: boolean;
  consecutiveAutoFailures: number;
  lastFailureCode?: string | null;
  pendingContinuation: boolean;
}


export interface ScheduledTaskInput {
  name: string;
  prompt: string;
  scheduleKind: "once" | "daily" | "weekly";
  localDate?: string | null;
  weekdays: number[];
  localTime: string;
  timezone: string;
  agentKind: AgentKind;
  modelId: string;
  reasoningEffort: ReasoningEffort;
  mcpServerIds: string[];
  skillIds: string[];
  permissionMode: PermissionMode;
}
