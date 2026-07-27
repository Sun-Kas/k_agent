import { appConfig } from "../config";
import type {
  AgUiEvent,
  AgUiRunInput,
  HealthState,
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

export async function getMcpConfig(): Promise<{ path: string; source?: string; isTemplate?: boolean; servers: McpServerConfig[] }> {
  const response = await configFetch("/api/config/mcp");
  if (!response.ok) throw new Error(`Unable to load MCP config (${response.status})`);
  return response.json() as Promise<{ path: string; source?: string; isTemplate?: boolean; servers: McpServerConfig[] }>;
}

export async function saveMcpConfig(servers: McpServerConfig[]): Promise<void> {
  const response = await configFetch("/api/config/mcp", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ servers: servers.map(({ connected: _, ...server }) => server) })
  });
  if (!response.ok) throw new Error(`Unable to save MCP config (${response.status})`);
}

export async function getSkillsConfig(): Promise<{ path: string; skills: SkillConfig[] }> {
  const response = await configFetch("/api/config/skills");
  if (!response.ok) throw new Error(`Unable to load skills config (${response.status})`);
  return response.json() as Promise<{ path: string; skills: SkillConfig[] }>;
}

export async function saveSkillsConfig(skills: SkillConfig[]): Promise<void> {
  const response = await configFetch("/api/config/skills", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skills })
  });
  if (!response.ok) throw new Error(`Unable to save skills config (${response.status})`);
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

function parseSseFrame(frame: string): AgUiEvent | null {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith(appConfig.sseDataPrefix))
    .map((line) => line.slice(appConfig.sseDataPrefix.length))
    .join("\n");

  return data ? (JSON.parse(data) as AgUiEvent) : null;
}
