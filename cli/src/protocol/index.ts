export type ChatRole = "user" | "assistant" | "system" | "tool";
export type AgentKind = "k_agent" | "codex" | "claude_code" | string;
export type CliSessionMode = "ephemeral" | "resume";
export type PermissionMode = "default" | "full_access";
export type ReasoningEffort = "none" | "low" | "medium" | "high" | "max";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  meta?: {
    toolName?: string;
    runId?: string;
    toolCallId?: string;
  };
}

export interface SessionSummary {
  id: string;
  title: string;
  updatedAt: string;
  messageCount: number;
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
  sessionId: string;
  messages: ChatMessage[];
  trace: string[];
  tasks: string[];
  thinking: Array<Record<string, unknown>>;
  events?: AgUiEvent[];
  openInterrupts?: Array<Omit<ApprovalActivity, "sequence"> & {
    requestHash?: string;
    toolCallId?: string;
  }>;
  contextSummary?: string;
  contextStats?: Record<string, unknown>;
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
  | { type: "THINKING_START"; title?: string }
  | { type: "THINKING_TEXT_MESSAGE_START"; rawEvent?: unknown }
  | { type: "THINKING_TEXT_MESSAGE_CONTENT"; delta: string; rawEvent?: unknown }
  | { type: "THINKING_TEXT_MESSAGE_END"; rawEvent?: unknown }
  | { type: "THINKING_END" }
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
    modelId?: string;
    mcpServerIds?: string[];
    skillIds?: string[];
    reasoningEffort?: ReasoningEffort;
    agentKind?: AgentKind;
    agentOptions?: {
      cliSessionMode?: CliSessionMode;
      permissionMode?: PermissionMode;
      networkAccess?: boolean;
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
