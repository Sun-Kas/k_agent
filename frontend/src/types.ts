/**
 * 聊天 / AG-UI / 配置相关共享类型。
 * SessionState：服务端会话快照；AgUiEvent / AgUiRunInput：流式协议与请求体。
 */

export type ChatRole = "user" | "assistant" | "system" | "tool";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  meta?: {
    toolName?: string;
    runId?: string;
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

/** 打开会话时的服务端快照：messages 供模型上下文；events 供 UI 时间线重放。 */
export interface SessionState {
  sessionId: string;
  messages: ChatMessage[];
  trace: string[];
  tasks: string[];
  thinking: ThinkingActivity[];
  events?: AgUiEvent[];
  contextSummary?: string;
  compactedMessageIds?: string[];
  contextStats?: Record<string, unknown>;
  capabilities?: {
    mcpServerIds: string[];
    skillIds: string[];
    permissionMode?: PermissionMode;
  } | null;
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
  supportsReasoning: boolean;
  contextWindow?: number;
  maxOutputTokens?: number;
  contextSafetyTokens?: number;
  enabled: boolean;
  isNew?: boolean;
}

export type ReasoningEffort = "none" | "low" | "medium" | "high" | "max";

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
}

export interface RuntimeOption {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
}

export interface McpCapabilities {
  tools: Array<{ server_id: string; name: string; description?: string | null; input_schema?: Record<string, unknown> | null }>;
  resources: Record<string, Array<Record<string, unknown>>>;
  prompts: Record<string, Array<Record<string, unknown>>>;
}

/** AG-UI / 扩展 SSE 事件联合类型；App.applyAgUiEvent 按 type 投影到 UI。 */
export type AgUiEvent =
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | { type: "RUN_FINISHED"; threadId: string; runId: string; result?: unknown }
  | { type: "RUN_ERROR"; message: string; code?: string }
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
  | {
      type: "TOOL_CALL_RESULT";
      messageId: string;
      toolCallId: string;
      content: string;
      role?: "tool";
    }
  | { type: "CUSTOM"; name: string; value: Record<string, unknown> };

export interface ThinkingActivity {
  id: string;
  phase: "analysis" | "reasoning" | "tool" | "synthesis" | "complete";
  title: string;
  detail: string;
  status: "active" | "complete" | "error";
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
  forwardedProps: {
    modelId?: string;
    mcpServerIds?: string[];
    skillIds?: string[];
    reasoningEffort?: ReasoningEffort;
    attachments?: Array<{ name: string; dataUrl: string; type: string }>;
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
  status: "preparing" | "running" | "waiting" | "complete" | "error";
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
  status: "pending" | "submitting" | "approved" | "denied" | "cancelled" | "error";
  sequence: number;
  error?: string;
}
