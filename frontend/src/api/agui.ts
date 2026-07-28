import { appConfig } from "../config";
import type {
  AgUiEvent,
  AgUiRunInput,
  HealthState,
  McpCapabilities,
  McpServerConfig,
  ModelProfile,
  SkillConfig,
  SessionState,
  SessionSummary
} from "../types";

const apiUrl = (path: string) => `${appConfig.apiBaseUrl}${path}`;
const configFetch = (path: string, init?: RequestInit) =>
  fetch(apiUrl(path), { ...init, signal: AbortSignal.timeout(5000) });

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

export async function saveMcpConfig(servers: McpServerConfig[]): Promise<void> {
  const response = await configFetch("/api/config/mcp", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ servers: servers.map(({ connected: _, status: __, scope: ___, transport: ____, toolCount: _____, resourceCount: ______, error: _______, ...server }) => server) })
  });
  if (!response.ok) throw new Error(`Unable to save MCP config (${response.status})`);
}

export async function getMcpCapabilities(): Promise<McpCapabilities> {
  const response = await configFetch("/api/mcp/capabilities");
  if (!response.ok) throw new Error(`Unable to load MCP capabilities (${response.status})`);
  return response.json() as Promise<McpCapabilities>;
}

export async function reloadMcp(): Promise<void> {
  const response = await configFetch("/api/mcp/reload", { method: "POST" });
  if (!response.ok) throw new Error(`Unable to reload MCP (${response.status})`);
}

export async function getSkillsConfig(): Promise<{ path: string; projectSkillDir?: string; skills: SkillConfig[]; loadedSkills?: SkillConfig[] }> {
  const response = await configFetch("/api/config/skills");
  if (!response.ok) throw new Error(`Unable to load skills config (${response.status})`);
  return response.json() as Promise<{ path: string; projectSkillDir?: string; skills: SkillConfig[]; loadedSkills?: SkillConfig[] }>;
}

export async function saveSkillsConfig(skills: SkillConfig[]): Promise<void> {
  const response = await configFetch("/api/config/skills", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skills })
  });
  if (!response.ok) throw new Error(`Unable to save skills config (${response.status})`);
}

export async function importProjectSkill(file: File): Promise<{ id: string; name: string; filePath: string; skills?: SkillConfig[] }> {
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
  return response.json() as Promise<{ id: string; name: string; filePath: string; skills?: SkillConfig[] }>;
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
