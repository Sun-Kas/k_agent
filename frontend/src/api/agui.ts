/**
 * Access Layer HTTP 客户端：会话 CRUD、配置、审批，以及 AG-UI SSE 流式 run。
 * streamAgentRun 是聊天主路径的网络入口；事件由调用方（App.applyAgUiEvent）投影到 UI。
 */
import { appConfig } from "../config";
import { yieldAfterStreamBatch } from "./stream-scheduler";
import type {
  AgUiEvent,
  AgUiRunInput,
  AgentsCatalog,
  HealthState,
  McpCapabilities,
  McpServerConfig,
  ModelProfile,
  RuntimeOption,
  SkillConfig,
  SessionState,
  SessionSummary,
  SessionWorkspaceFileContent,
  SessionWorkspaceListing
} from "../types";

const apiUrl = (path: string) => `${appConfig.apiBaseUrl}${path}`;
const configFetch = (path: string, init?: RequestInit, timeoutMs = 5000) =>
  fetch(apiUrl(path), { ...init, signal: AbortSignal.timeout(timeoutMs) });

export async function listSessions(): Promise<SessionSummary[]> {
  const response = await fetch(apiUrl("/api/sessions"));
  if (!response.ok) {
    throw new Error(`Unable to load sessions (${response.status})`);
  }
  return response.json() as Promise<SessionSummary[]>;
}

export async function getSessionWorkspace(sessionId: string): Promise<SessionWorkspaceListing> {
  const response = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/workspace`));
  if (!response.ok) {
    throw new Error(`Unable to load session workspace (${response.status})`);
  }
  return response.json() as Promise<SessionWorkspaceListing>;
}

export async function getSessionWorkspaceFile(
  sessionId: string,
  path: string
): Promise<SessionWorkspaceFileContent> {
  const query = new URLSearchParams({ path });
  const response = await fetch(
    apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/workspace/file?${query}`)
  );
  if (!response.ok) {
    throw new Error(`Unable to read workspace file (${response.status})`);
  }
  return response.json() as Promise<SessionWorkspaceFileContent>;
}

export async function getHealth(): Promise<HealthState> {
  const response = await fetch(apiUrl("/api/health"));
  if (!response.ok) {
    throw new Error(`Unable to check service health (${response.status})`);
  }
  return response.json() as Promise<HealthState>;
}

export async function getModelsConfig(): Promise<{ path: string; source: string; models: ModelProfile[] }> {
  const response = await configFetch("/api/config/models");
  if (!response.ok) throw new Error(`Unable to load models config (${response.status})`);
  return response.json() as Promise<{ path: string; source: string; models: ModelProfile[] }>;
}

export async function getRuntimeCatalog(): Promise<{
  mcpServers: RuntimeOption[];
  skills: RuntimeOption[];
  sources: { mcp: string; skills: string };
}> {
  const response = await configFetch("/api/catalog");
  if (!response.ok) throw new Error(`Unable to load runtime catalog (${response.status})`);
  return response.json() as Promise<{
    mcpServers: RuntimeOption[];
    skills: RuntimeOption[];
    sources: { mcp: string; skills: string };
  }>;
}

export async function getAgentsCatalog(): Promise<AgentsCatalog> {
  const response = await configFetch("/api/agents");
  if (!response.ok) throw new Error(`Unable to load agents catalog (${response.status})`);
  return response.json() as Promise<AgentsCatalog>;
}

export async function saveModelsConfig(models: ModelProfile[]): Promise<void> {
  const response = await configFetch("/api/config/models", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      models: models.map(({ apiKeyConfigured: _, ...model }) => model)
    })
  });
  if (!response.ok) throw new Error(`Unable to save models config (${response.status})`);
}

export async function getMcpConfig(): Promise<{ path: string; source?: string; format?: string; isTemplate?: boolean; servers: McpServerConfig[]; warnings?: string[]; blocked?: string[]; suppressed?: Array<Record<string, string>> }> {
  const response = await configFetch("/api/config/mcp");
  if (!response.ok) throw new Error(`Unable to load MCP config (${response.status})`);
  return response.json() as Promise<{ path: string; source?: string; format?: string; isTemplate?: boolean; servers: McpServerConfig[]; warnings?: string[]; blocked?: string[]; suppressed?: Array<Record<string, string>> }>;
}

export async function saveMcpConfig(servers: McpServerConfig[]): Promise<{
  ok: boolean;
  restartRequired: boolean;
  servers: McpServerConfig[];
  warnings?: string[];
  blocked?: string[];
}> {
  const response = await configFetch("/api/config/mcp", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ servers: servers.map(sanitizeMcpServerForSave) })
  }, 120000);
  if (!response.ok) {
    throw new Error(await configResponseError(response, "MCP 配置保存失败"));
  }
  return response.json() as Promise<{
    ok: boolean;
    restartRequired: boolean;
    servers: McpServerConfig[];
    warnings?: string[];
    blocked?: string[];
  }>;
}

/** 按 transport 裁剪字段，避免把 stdio/http 互斥配置一并写回。 */
function sanitizeMcpServerForSave(server: McpServerConfig): McpServerConfig {
  const type = server.type ?? "stdio";
  const optional = (value?: string) => value?.trim() || undefined;
  const cleanRecord = (value?: Record<string, string>) => Object.fromEntries(
    Object.entries(value ?? {})
      .filter(([key]) => key.trim())
      .map(([key, itemValue]) => [key.trim(), itemValue])
  );
  return {
    id: server.id.trim(),
    name: optional(server.name),
    description: server.description ?? "",
    type,
    command: type === "stdio" ? optional(server.command) : undefined,
    args: type === "stdio"
      ? (server.args ?? []).map((item) => item.trim()).filter(Boolean)
      : [],
    env: type === "stdio" ? cleanRecord(server.env) : {},
    envPassthrough: type === "stdio"
      ? (server.envPassthrough ?? []).map((item) => item.trim()).filter(Boolean)
      : [],
    cwd: type === "stdio" ? optional(server.cwd) : undefined,
    url: type === "http" ? optional(server.url) : undefined,
    bearerTokenEnv: type === "http" ? optional(server.bearerTokenEnv) : undefined,
    headers: type === "http" ? cleanRecord(server.headers) : {},
    envHeaders: type === "http" ? cleanRecord(server.envHeaders) : {},
    enabled: server.enabled
  };
}

async function configResponseError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json() as {
      detail?: string | Array<{ loc?: Array<string | number>; msg?: string }>;
    };
    if (typeof payload.detail === "string" && payload.detail.trim()) {
      return `${fallback}：${payload.detail}`;
    }
    if (Array.isArray(payload.detail)) {
      const issues = payload.detail.map((issue) => {
        const field = issue.loc?.slice(1).join(".") || "请求字段";
        return `${field} ${issue.msg || "格式不正确"}`;
      });
      if (issues.length) return `${fallback}：${issues.join("；")}`;
    }
  } catch {
    // Some proxy errors are not JSON; preserve a useful status fallback.
  }
  return `${fallback}（${response.status}）`;
}

export async function getMcpCapabilities(): Promise<McpCapabilities> {
  const response = await configFetch("/api/mcp/capabilities");
  if (!response.ok) throw new Error(`Unable to load MCP capabilities (${response.status})`);
  return response.json() as Promise<McpCapabilities>;
}

export async function reloadMcp(): Promise<void> {
  const response = await configFetch("/api/mcp/reload", { method: "POST" }, 120000);
  if (!response.ok) throw new Error(`Unable to reload MCP (${response.status})`);
}

export async function resolveApproval(
  requestId: string,
  input: {
    threadId: string;
    runId: string;
    action: "approve" | "deny" | "cancel";
    remember?: boolean;
    answers?: Record<string, string[]>;
    content?: Record<string, unknown>;
  }
): Promise<void> {
  const response = await fetch(apiUrl(`/api/approvals/${encodeURIComponent(requestId)}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input)
  });
  if (!response.ok) {
    throw new Error(await configResponseError(response, "审批提交失败"));
  }
}

export async function getApprovalStatus(
  requestId: string,
  input: { threadId: string; runId: string }
): Promise<{ pending: boolean }> {
  const query = new URLSearchParams(input);
  const response = await fetch(apiUrl(
    `/api/approvals/${encodeURIComponent(requestId)}?${query.toString()}`
  ));
  if (!response.ok) throw new Error(await configResponseError(response, "审批状态查询失败"));
  return response.json() as Promise<{ pending: boolean }>;
}

export async function getSkillsConfig(): Promise<{ path: string; skillDir?: string; skills: SkillConfig[]; loadedSkills?: SkillConfig[] }> {
  const response = await configFetch("/api/config/skills");
  if (!response.ok) throw new Error(`Unable to load skills config (${response.status})`);
  return response.json() as Promise<{ path: string; skillDir?: string; skills: SkillConfig[]; loadedSkills?: SkillConfig[] }>;
}

export async function saveSkillsConfig(skills: SkillConfig[]): Promise<void> {
  const response = await configFetch("/api/config/skills", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skills })
  });
  if (!response.ok) throw new Error(`Unable to save skills config (${response.status})`);
}

export async function importSkill(file: File): Promise<{ id: string; name: string; filePath: string; skills?: SkillConfig[]; loadedSkills?: SkillConfig[] }> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(apiUrl("/api/skills"), {
    method: "POST",
    body,
    signal: AbortSignal.timeout(30000)
  });
  if (!response.ok) {
    let detail = `Unable to import skill (${response.status})`;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep the status-only message when the server did not return JSON.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<{ id: string; name: string; filePath: string; skills?: SkillConfig[]; loadedSkills?: SkillConfig[] }>;
}

export async function getSession(sessionId: string): Promise<SessionState> {
  const response = await fetch(apiUrl(`/api/sessions/${sessionId}`));
  if (!response.ok) {
    throw new Error(`Unable to load session (${response.status})`);
  }
  return response.json() as Promise<SessionState>;
}

export async function cancelSessionRun(sessionId: string, runId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/runs/cancel`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId })
  });
  if (!response.ok) {
    throw new Error(`Unable to cancel session run (${response.status})`);
  }
}

/** Manual stop is a durable terminal boundary; unlike cancel it preserves this turn. */
export async function stopSessionRun(sessionId: string, runId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/runs/stop`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId })
  });
  if (!response.ok) {
    throw new Error(`Unable to stop session run (${response.status})`);
  }
}

/**
 * POST AgUiRunInput，按 SSE 帧解析为 AgUiEvent 并回调。
 * 每批完整 SSE 帧投影后 await nextRenderOpportunity，给 React 绘制喘息。
 * 这不只服务文本 delta：工具、思考和审批卡片也必须在 run 暂停前立刻可见。
 */
export async function streamAgentRun(
  input: AgUiRunInput,
  onEvent: (event: AgUiEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch(apiUrl(appConfig.agUiEndpoint), {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json"
    },
    body: JSON.stringify(input),
    signal
  });

  if (!response.ok) {
    throw new Error(`Agent request failed (${response.status})`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Streaming response is unavailable");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    // 不完整帧留在 buffer；完整帧以空行分隔。
    const frames = buffer.split(appConfig.sseMessageDelimiter);
    buffer = frames.pop() ?? "";
    let dispatchedVisibleEvent = false;

    for (const frame of frames) {
      const event = parseSseFrame(frame);
      if (event) {
        onEvent(event);
        dispatchedVisibleEvent = true;
      }
    }

    // React 可能把同一读取循环中的多次 setState 合并。按网络批次只让出
    // 一次绘制机会，既能实时显示审批/工具卡片，也不会让同批事件逐个跳帧。
    await yieldAfterStreamBatch(dispatchedVisibleEvent);

    if (done) {
      const finalEvent = parseSseFrame(buffer);
      if (finalEvent) {
        onEvent(finalEvent);
        await yieldAfterStreamBatch(true);
      }
      return;
    }
  }
}

/** 提取 `data: ` 行并 JSON.parse 为 AgUiEvent；无 data 行则忽略（注释/心跳）。 */
function parseSseFrame(frame: string): AgUiEvent | null {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith(appConfig.sseDataPrefix))
    .map((line) => line.slice(appConfig.sseDataPrefix.length))
    .join("\n");

  return data ? (JSON.parse(data) as AgUiEvent) : null;
}
