import { appConfig } from "../config";
import type {
  AgUiEvent,
  AgUiRunInput,
  HealthState,
  McpCapabilities,
  McpServerConfig,
  ModelProfile,
  RuntimeOption,
  SkillConfig,
  SessionState,
  SessionSummary
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
    const frames = buffer.split(appConfig.sseMessageDelimiter);
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const event = parseSseFrame(frame);
      if (event) {
        onEvent(event);
        if (event.type === "TEXT_MESSAGE_CONTENT") {
          await nextPaint();
        }
      }
    }

    if (done) {
      const finalEvent = parseSseFrame(buffer);
      if (finalEvent) {
        onEvent(finalEvent);
      }
      return;
    }
  }
}

function nextPaint(): Promise<void> {
  if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
    return Promise.resolve();
  }
  return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}

function parseSseFrame(frame: string): AgUiEvent | null {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith(appConfig.sseDataPrefix))
    .map((line) => line.slice(appConfig.sseDataPrefix.length))
    .join("\n");

  return data ? (JSON.parse(data) as AgUiEvent) : null;
}
