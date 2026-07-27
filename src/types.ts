export type ChatRole = "user" | "assistant" | "system" | "tool";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  meta?: {
    toolName?: string;
  };
}

export interface AgentResponse {
  sessionId: string;
  messages: ChatMessage[];
  trace: string[];
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
}

export interface HealthState {
  ok: boolean;
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

export interface McpServerConfig {
  id: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  enabled: boolean;
  connected?: boolean;
}

export interface SkillConfig {
  id: string;
  name: string;
  description: string;
  instructions: string;
  enabled: boolean;
}

export type AgUiEvent =
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | { type: "RUN_FINISHED"; threadId: string; runId: string; result?: unknown }
  | { type: "RUN_ERROR"; message: string; code?: string }
  | { type: "TEXT_MESSAGE_START"; messageId: string; role: "assistant" }
  | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "TEXT_MESSAGE_END"; messageId: string }
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
    reasoningEffort?: "none" | "low" | "medium" | "high";
    attachments?: Array<{ name: string; dataUrl: string; type: string }>;
  };
}

export interface ToolActivity {
  id: string;
  name: string;
  arguments: string;
  result?: string;
  status: "preparing" | "running" | "complete";
}
