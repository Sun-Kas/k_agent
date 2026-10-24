/**
 * 聊天 / AG-UI / 配置相关共享类型。
 * SessionState：服务端会话快照；AgUiEvent / AgUiRunInput：流式协议与请求体。
 */

export type ChatRole = "user" | "assistant" | "system" | "tool";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  attachments?: MediaAttachment[];
  createdAt: string;
  meta?: {
    toolName?: string;
    runId?: string;
  };
}

export interface MediaAttachment {
  /** 原始文件名，例如 cat.png */
  name: string;
  /** FileReader.readAsDataURL：data:{type};base64, 后面是文件字节的 Base64，不是 URL 也不是像素数组。 */
  dataUrl: string;
  /** MIME，仅 image/* 或 video/* */
  type: string;
}

export interface SessionSummary {
  id: string;
  title: string;
  updatedAt: string;
  messageCount: number;
  /** 归属的 Project id；undefined/null 表示未归属任何 Project。 */
  projectId?: string | null;
  environment?: SessionEnvironment | null;
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

/** 打开会话时的服务端快照：UI 只重放完整 history 事件。 */
export interface SessionState {
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
  environment?: SessionEnvironment | null;
}

export interface BashSandboxHealth {
  available: boolean;
  mode: string;
  command: string;
  reason: string;
  needsInstall: boolean;
  platform?: string;
  userSummary?: string;
  manualInstallCommand?: string;
  agentInstallTool?: string;
}

export interface HealthState {
  ok: boolean;
  agentBackendOk: boolean;
  model: string;
  localToolCount: number;
  mcpToolCount: number;
  bashSandbox?: BashSandboxHealth | null;
}

export interface ModelProfile {
  id: string;
  name: string;
  model: string;
  baseUrl: string;
  apiKeyConfigured: boolean;
  apiKey?: string;
  apiKeyEnv?: string;
  multimodal: boolean;
  inputModalities?: Array<"text" | "image" | "video">;
  supportsReasoning: boolean;
  contextWindow?: number;
  maxOutputTokens?: number;
  contextSafetyTokens?: number;
  autoCompactEnabled?: boolean;
  compactModelId?: string | null;
  enabled: boolean;
  isNew?: boolean;
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

export type ReasoningEffort = "none" | "low" | "medium" | "high" | "max";

export interface MarketplaceOrigin {
  source: string;
  sourceId: string;
  version?: string | null;
  installedAt?: string | null;
  iconUrl?: string | null;
}

export interface McpServerConfig {
  id: string;
  name?: string;
  description?: string;
  type?: "stdio" | "http";
  command?: string;
  args: string[];
  env: Record<string, string>;
  envPassthrough?: string[];
  cwd?: string;
  url?: string;
  bearerTokenEnv?: string;
  headers?: Record<string, string>;
  envHeaders?: Record<string, string>;
  enabled: boolean;
  connected?: boolean;
  status?: "connected" | "failed" | "disabled" | "pending" | "unknown";
  scope?: string;
  transport?: string;
  toolCount?: number;
  resourceCount?: number;
  error?: string | null;
  isNew?: boolean;
  marketplace?: MarketplaceOrigin | null;
}

export interface SkillConfig {
  id: string;
  name: string;
  description: string;
  instructions: string;
  enabled: boolean;
  source?: string;
  loadedFrom?: string;
  filePath?: string | null;
  baseDir?: string | null;
  paths?: string[];
  whenToUse?: string | null;
  userInvocable?: boolean;
  editable?: boolean;
  isNew?: boolean;
  marketplace?: MarketplaceOrigin | null;
}

export interface RuntimeOption {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  marketplace?: MarketplaceOrigin | null;
}

export interface McpCapabilities {
  tools: Array<{ server_id: string; name: string; description?: string | null; input_schema?: Record<string, unknown> | null }>;
  resources: Record<string, Array<Record<string, unknown>>>;
  prompts: Record<string, Array<Record<string, unknown>>>;
}

/** AG-UI / 扩展 SSE 事件联合类型；App.applyAgUiEvent 按 type 投影到 UI。 */
export type AgUiEvent =
  | { type: "input_message"; runId?: string; message: ChatMessage }
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | {
      type: "RUN_FINISHED";
      threadId: string;
      runId: string;
      result?: unknown;
      outcome?: { type: "interrupt"; interrupts: AgUiInterrupt[] };
    }
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
  | {
      type: "TOOL_CALL_RESULT";
      messageId: string;
      toolCallId: string;
      content: string;
      role?: "tool";
    }
  | {
      type: "ACTIVITY_SNAPSHOT";
      messageId: string;
      activityType: string;
      content: unknown;
      replace?: boolean;
    }
  | { type: "CUSTOM"; name: string; value: Record<string, unknown> };

export interface ThinkingActivity {
  id: string;
  phase: "analysis" | "reasoning" | "tool" | "synthesis" | "complete";
  title: string;
  detail: string;
  status: "active" | "complete" | "error" | "stopped";
  iteration: number;
  createdAt: string;
}

/**
 * POST /api/agent 请求体。
 * messages 仅含本轮用户消息；模型/能力/agent 选项在 forwardedProps，历史由服务端按 threadId 补齐。
 */
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
    payload?: (
      | { approved: boolean; scope?: "once" | "run"; reconfirm?: boolean }
      | { answers: UserQuestionAnswers; reconfirm?: boolean }
    );
  }>;
  forwardedProps: {
    modelId?: string;
    /** 本轮勾选的 MCP / Skill 的 id，不是连接配置或 SKILL.md。Access Layer 按 catalog 展开后再转给 Backend。 */
    mcpServerIds?: string[];
    skillIds?: string[];
    reasoningEffort?: ReasoningEffort;
    /** 本轮图片/视频，走 Data URL；不进 messages。最多 4 个，每个 ≤20MB。 */
    attachments?: MediaAttachment[];
    agentKind?: AgentKind;
    agentOptions?: {
      voiceConversation?: boolean;
      voiceStyle?: VoiceStyleId;
      cliSessionMode?: CliSessionMode;
      resumeSessionId?: string;
      networkAccess?: boolean;
      permissionMode?: PermissionMode;
      claudeAutoApproveTools?: string[];
    };
  };
}

export type AgentKind = "k_agent" | "codex" | "claude_code" | string;
export type CliSessionMode = "ephemeral" | "resume";
/** default 保持沙箱并对越权动作发起 HITL；full_access 由用户显式承担宿主机风险。 */
export type PermissionMode = "default" | "full_access";
export type VoiceStyleId = "natural" | "warm" | "lively" | "professional" | "storytelling";

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

export interface ToolActivity {
  id: string;
  turnId: string;
  name: string;
  arguments: string;
  result?: string;
  liveOutput?: string;
  executionMode?: "foreground" | "interactive";
  status: "preparing" | "running" | "waiting" | "complete" | "error" | "stopped";
  sequence?: number;
  textOffset?: number;
}

export interface TextActivity {
  id: string;
  turnId: string;
  content: string;
  status: "streaming" | "complete";
  sequence: number;
}

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

export interface AgUiInterrupt {
  id: string;
  reason: string;
  message: string;
  toolCallId?: string;
  responseSchema?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}
