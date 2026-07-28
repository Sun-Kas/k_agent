export type ChatRole = "user" | "assistant" | "system" | "tool";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  meta?: {
    toolName?: string;
    thinkingGroups?: Array<{
      id: string;
      steps: ThinkingActivity[];
      closed: boolean;
      textStart?: number;
      textEnd?: number;
      sequence?: number;
    }>;
    toolActivities?: ToolActivity[];
    textActivities?: TextActivity[];
  };
}

export interface SessionSummary {
  id: string;
  title: string;
  updatedAt: string;
  messageCount: number;
}

export interface SessionState {
  sessionId: string;
  messages: ChatMessage[];
  trace: string[];
  tasks: string[];
  thinking: ThinkingActivity[];
  thinkingGroups?: Array<{
    id: string;
    steps: ThinkingActivity[];
    closed: boolean;
    textStart?: number;
    textEnd?: number;
  }>;
}

export interface HealthState {
  ok: boolean;
  agentBackendOk: boolean;
  model: string;
  localToolCount: number;
  mcpToolCount: number;
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
  enabled: boolean;
}

export type ReasoningEffort = "none" | "low" | "medium" | "high" | "max";

export interface McpServerConfig {
  id: string;
  type?: "stdio" | "sse" | "http" | "ws" | "sdk";
  command?: string;
  args: string[];
  env: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  enabled: boolean;
  connected?: boolean;
  status?: "connected" | "failed" | "disabled" | "pending" | "unknown";
  scope?: string;
  transport?: string;
  toolCount?: number;
  resourceCount?: number;
  error?: string | null;
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
}

export interface McpCapabilities {
  tools: Array<{ server_id: string; name: string; description?: string | null; input_schema?: Record<string, unknown> | null }>;
  resources: Record<string, Array<Record<string, unknown>>>;
  prompts: Record<string, Array<Record<string, unknown>>>;
}

export type AgUiEvent =
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | { type: "RUN_FINISHED"; threadId: string; runId: string; result?: unknown }
  | { type: "RUN_ERROR"; message: string; code?: string }
  | { type: "TEXT_MESSAGE_START"; messageId: string; role: "assistant" }
  | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "TEXT_MESSAGE_END"; messageId: string }
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
  | { type: "STATE_SNAPSHOT"; snapshot: AgentState }
  | { type: "CUSTOM"; name: string; value: Record<string, unknown> };

export interface AgentState {
  sessionId: string;
  messages: ChatMessage[];
  trace: string[];
  tasks: string[];
  thinking: ThinkingActivity[];
  thinkingGroups?: Array<{
    id: string;
    steps: ThinkingActivity[];
    closed: boolean;
  }>;
}

export interface ThinkingActivity {
  id: string;
  phase: "analysis" | "reasoning" | "tool" | "synthesis" | "complete";
  title: string;
  detail: string;
  status: "active" | "complete" | "error";
  iteration: number;
  createdAt: string;
}

export interface AgUiRunInput {
  threadId: string;
  runId: string;
  state: {
    sessionId: string | null;
    trace: string[];
    tasks: string[];
  };
  messages: Array<Pick<ChatMessage, "id" | "role" | "content">>;
  tools: unknown[];
  context: unknown[];
  forwardedProps: {
    modelId?: string;
    mcpServerIds?: string[];
    skillIds?: string[];
    reasoningEffort?: ReasoningEffort;
    attachments?: Array<{ name: string; dataUrl: string; type: string }>;
  };
}

export interface ToolActivity {
  id: string;
  name: string;
  arguments: string;
  result?: string;
  status: "preparing" | "running" | "waiting" | "complete";
  sequence?: number;
  textOffset?: number;
}

export interface TextActivity {
  id: string;
  content: string;
  status: "streaming" | "complete";
  sequence: number;
}
