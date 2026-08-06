import { appConfig } from "../config";
import type {
  TeamAgentDraft,
  TeamEvent,
  TeamSnapshot,
  TeamSummary
} from "./types";

export interface TeamWorkspaceFile {
  path: string;
  name: string;
  size: number;
  modifiedAt: number;
}

export interface TeamWorkspaceListing {
  teamId: string;
  root: string;
  files: TeamWorkspaceFile[];
}

export interface TeamWorkspaceFileContent {
  teamId: string;
  path: string;
  name: string;
  content: string;
  truncated: boolean;
  binary: boolean;
  size: number;
}

const teamUrl = (path: string) => `${appConfig.apiBaseUrl}/api/teams${path}`;

async function teamFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(teamUrl(path), {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers }
  });
  if (!response.ok) {
    let detail = `Team API request failed (${response.status})`;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Preserve the status fallback for proxy or HTML errors.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const listTeams = () => teamFetch<TeamSummary[]>("");
export const getTeam = (teamId: string) => teamFetch<TeamSnapshot>(`/${encodeURIComponent(teamId)}`);
export const getTeamEvents = (teamId: string, afterSeq = 0, limit = 2000) =>
  teamFetch<TeamEvent[]>(
    `/${encodeURIComponent(teamId)}/events?afterSeq=${afterSeq}&limit=${limit}`
  );
export const getTeamTaskEvents = (teamId: string, taskId: string, limit = 5000) =>
  teamFetch<TeamEvent[]>(
    `/${encodeURIComponent(teamId)}/events?${new URLSearchParams({
      taskId,
      limit: String(limit)
    })}`
  );

export const getTeamWorkspace = (teamId: string) =>
  teamFetch<TeamWorkspaceListing>(`/${encodeURIComponent(teamId)}/workspace`);

export const getTeamWorkspaceFile = (teamId: string, path: string) =>
  teamFetch<TeamWorkspaceFileContent>(
    `/${encodeURIComponent(teamId)}/workspace/file?${new URLSearchParams({ path })}`
  );

export function createTeam(input: {
  name: string;
  goal: string;
  workspaceDir?: string;
  mode: "auto" | "manual";
  maxParallel: number;
  agents: TeamAgentDraft[];
}): Promise<TeamSnapshot> {
  return teamFetch<TeamSnapshot>("", {
    method: "POST",
    body: JSON.stringify({
      ...input,
      agents: input.agents.map((agent) => ({
        name: agent.name,
        role: agent.role,
        agentKind: agent.agentKind,
        modelId: agent.modelId || undefined,
        reasoningEffort: agent.reasoningEffort || undefined,
        networkAccess: agent.networkAccess ?? undefined,
        responsibility: agent.responsibility,
        isSupervisor: agent.isSupervisor,
        mcpServerIds: agent.mcpServerIds,
        skillIds: agent.skillIds
      }))
    })
  });
}

export const commandTeam = (teamId: string, command: "pause" | "resume" | "cancel") =>
  teamFetch<TeamSnapshot>(`/${encodeURIComponent(teamId)}/commands`, {
    method: "POST",
    body: JSON.stringify({ command })
  });

export const sendTeamMessage = (teamId: string, recipientId: string, content: string) =>
  teamFetch<TeamSnapshot>(`/${encodeURIComponent(teamId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ senderId: "user", recipientId, messageType: "user_message", content })
  });

export function subscribeTeamEvents(
  teamId: string,
  afterSeq: number,
  onEvent: (event: TeamEvent) => void,
  onConnection: (connected: boolean) => void
): () => void {
  const source = new EventSource(teamUrl(`/${encodeURIComponent(teamId)}/stream?afterSeq=${afterSeq}`));
  source.onopen = () => onConnection(true);
  source.onerror = () => onConnection(false);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as TeamEvent);
    } catch {
      // A malformed external/proxy event must not break later Team updates.
    }
  };
  return () => source.close();
}
