import { AccessLayerError, responseError } from "./errors.js";
import { parseAgUiSse } from "./sse-parser.js";
import type {
  AgentsCatalog,
  AgUiEvent,
  AgUiRunInput,
  HealthState,
  ModelProfile,
  RuntimeCatalog,
  ScheduledTaskSummary,
  ScheduledRunSummary,
  SessionState,
  SessionSummary,
  SessionWorkspaceFileContent,
  SessionWorkspaceListing,
  TeamSummary,
} from "../protocol/index.js";

export class AccessLayerClient {
  readonly endpoint: string;

  constructor(endpoint: string) {
    this.endpoint = endpoint.replace(/\/$/, "");
  }

  async health(): Promise<HealthState> {
    return this.json<HealthState>("/api/health", undefined, 5_000);
  }

  async catalog(): Promise<RuntimeCatalog> {
    return this.json<RuntimeCatalog>("/api/catalog", undefined, 5_000);
  }

  async agents(): Promise<AgentsCatalog> {
    return this.json<AgentsCatalog>("/api/agents", undefined, 5_000);
  }

  async models(): Promise<ModelProfile[]> {
    const payload = await this.json<{ models: ModelProfile[] }>("/api/config/models", undefined, 5_000);
    return payload.models;
  }

  async listSessions(): Promise<SessionSummary[]> {
    return this.json<SessionSummary[]>("/api/sessions");
  }

  async getSession(sessionId: string): Promise<SessionState> {
    return this.json<SessionState>(`/api/sessions/${encodeURIComponent(sessionId)}`);
  }

  async forkSession(sessionId: string): Promise<SessionSummary> {
    return this.json<SessionSummary>(`/api/sessions/${encodeURIComponent(sessionId)}/fork`, { method: "POST" });
  }

  async deleteSession(sessionId: string): Promise<void> {
    await this.request(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  }

  async stopRun(sessionId: string, runId: string): Promise<void> {
    await this.json(`/api/sessions/${encodeURIComponent(sessionId)}/runs/stop`, jsonPost({ runId }));
  }

  async cancelRun(sessionId: string, runId: string): Promise<void> {
    await this.json(`/api/sessions/${encodeURIComponent(sessionId)}/runs/cancel`, jsonPost({ runId }));
  }

  async workspace(sessionId: string): Promise<SessionWorkspaceListing> {
    return this.json<SessionWorkspaceListing>(`/api/sessions/${encodeURIComponent(sessionId)}/workspace`);
  }

  async workspaceFile(sessionId: string, path: string): Promise<SessionWorkspaceFileContent> {
    const query = new URLSearchParams({ path });
    return this.json<SessionWorkspaceFileContent>(
      `/api/sessions/${encodeURIComponent(sessionId)}/workspace/file?${query}`,
    );
  }

  async listTeams(): Promise<TeamSummary[]> {
    return this.json<TeamSummary[]>("/api/teams");
  }

  async getTeam(teamId: string): Promise<TeamSummary> {
    return this.json<TeamSummary>(`/api/teams/${encodeURIComponent(teamId)}`);
  }

  async commandTeam(teamId: string, command: "pause" | "resume" | "cancel"): Promise<TeamSummary> {
    return this.json<TeamSummary>(`/api/teams/${encodeURIComponent(teamId)}/commands`, jsonPost({ command }));
  }

  async listScheduledTasks(): Promise<ScheduledTaskSummary[]> {
    return this.json<ScheduledTaskSummary[]>("/api/scheduled-tasks");
  }

  async getScheduledTask(taskId: string): Promise<ScheduledTaskSummary> {
    return this.json<ScheduledTaskSummary>(`/api/scheduled-tasks/${encodeURIComponent(taskId)}`);
  }

  async scheduledTaskAction(taskId: string, action: "pause" | "resume" | "run-now"): Promise<unknown> {
    return this.json(`/api/scheduled-tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST" });
  }

  async listScheduledRuns(taskId: string): Promise<ScheduledRunSummary[]> {
    return this.json<ScheduledRunSummary[]>(`/api/scheduled-tasks/${encodeURIComponent(taskId)}/runs`);
  }

  async *streamRun(input: AgUiRunInput, signal?: AbortSignal): AsyncGenerator<AgUiEvent> {
    // 连接超时只约束收到响应头之前的阶段；SSE 建立后不能沿用短超时，
    // 否则长任务会被 CLI 在固定时间后误判为网络失败。
    const connectController = new AbortController();
    const connectTimer = setTimeout(() => connectController.abort(), 10_000);
    const requestSignal = signal
      ? AbortSignal.any([signal, connectController.signal])
      : connectController.signal;
    let response: Response;
    try {
      response = await fetch(`${this.endpoint}/api/agent`, {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(input),
        signal: requestSignal,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      if (connectController.signal.aborted) {
        throw new AccessLayerError("连接 Agent 事件流超时（10000ms）", { code: "timeout", cause: error });
      }
      throw new AccessLayerError(`无法连接 Access Layer：${this.endpoint}`, { code: "network", cause: error });
    } finally {
      clearTimeout(connectTimer);
    }
    if (!response.ok) throw await responseError(response, "Agent 事件流请求失败");
    yield* parseAgUiSse(response);
  }

  private async json<T = unknown>(path: string, init?: RequestInit, timeoutMs = 10_000): Promise<T> {
    const response = await this.request(path, init, timeoutMs);
    if (response.status === 204) return undefined as T;
    try {
      return await response.json() as T;
    } catch (error) {
      throw new AccessLayerError("Access Layer 返回了无效 JSON", { code: "protocol", cause: error });
    }
  }

  private async request(path: string, init: RequestInit = {}, timeoutMs = 10_000): Promise<Response> {
    const timeoutSignal = AbortSignal.timeout(timeoutMs);
    const signal = init.signal ? AbortSignal.any([init.signal, timeoutSignal]) : timeoutSignal;
    let response: Response;
    try {
      response = await fetch(`${this.endpoint}${path}`, { ...init, signal });
    } catch (error) {
      if (signal.aborted && !init.signal?.aborted) {
        throw new AccessLayerError(`请求 Access Layer 超时（${timeoutMs}ms）`, { code: "timeout", cause: error });
      }
      if (init.signal?.aborted) throw error;
      throw new AccessLayerError(`无法连接 Access Layer：${this.endpoint}`, { code: "network", cause: error });
    }
    if (!response.ok) throw await responseError(response, "Access Layer 请求失败");
    return response;
  }
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
