import { AccessLayerError, responseError } from "./errors.js";
import { parseAgUiSse } from "./sse-parser.js";
import type {
  AgentsCatalog,
  ProjectSummary,
  ProjectRepository,
  DirectoryListing,
  SessionEnvironment,
  SessionChanges,
  SessionContextStatus,
  IntegrationState,
  IntegrationFile,
  ScheduledTaskInput,
  AgUiEvent,
  AgUiRunInput,
  HealthState,
  McpCapabilities,
  McpConfigPayload,
  McpServerConfig,
  ModelProfile,
  RuntimeCatalog,
  SkillConfigItem,
  SkillsConfigPayload,
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

  async getMcpConfig(): Promise<McpConfigPayload> {
    return this.json<McpConfigPayload>("/api/config/mcp", undefined, 10_000);
  }

  async saveMcpConfig(servers: McpServerConfig[]): Promise<McpConfigPayload> {
    return this.json<McpConfigPayload>("/api/config/mcp", jsonPut({ servers }), 120_000);
  }

  async reloadMcp(): Promise<void> {
    await this.json("/api/mcp/reload", { method: "POST" }, 120_000);
  }

  async mcpCapabilities(): Promise<McpCapabilities> {
    return this.json<McpCapabilities>("/api/mcp/capabilities", undefined, 15_000);
  }

  async agents(): Promise<AgentsCatalog> {
    return this.json<AgentsCatalog>("/api/agents", undefined, 5_000);
  }

  async models(): Promise<ModelProfile[]> {
    const payload = await this.json<{ models: ModelProfile[] }>("/api/config/models", undefined, 5_000);
    return payload.models;
  }

  async getSkillsConfig(): Promise<SkillsConfigPayload> {
    return this.json<SkillsConfigPayload>("/api/config/skills", undefined, 10_000);
  }

  async saveSkillsConfig(skills: SkillConfigItem[]): Promise<SkillsConfigPayload> {
    return this.json<SkillsConfigPayload>("/api/config/skills", jsonPut({ skills }), 120_000);
  }

  async listSessions(): Promise<SessionSummary[]> {
    return this.json<SessionSummary[]>("/api/sessions");
  }

  async getSession(sessionId: string): Promise<SessionState> {
    return this.json<SessionState>(`/api/sessions/${encodeURIComponent(sessionId)}`);
  }

  async forkSession(sessionId: string, anchor?: { throughMessageId?: string; throughRunId?: string; environment?: "same-directory" | "worktree" }): Promise<SessionSummary> {
    return this.json<SessionSummary>(`/api/sessions/${encodeURIComponent(sessionId)}/fork`, jsonPost(anchor ?? {} ));
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

  async listProjects(): Promise<ProjectSummary[]> { return this.json("/api/projects"); }
  async createProject(rootPath: string, name?: string): Promise<ProjectSummary> { return this.json("/api/projects", jsonPost({ rootPath, name })); }
  async updateProject(projectId: string, name: string, targetBranch?: string | null): Promise<ProjectSummary> {
    return this.json(`/api/projects/${encodeURIComponent(projectId)}`, { ...jsonPost({ name, ...(targetBranch === undefined ? {} : { targetBranch }) }), method: "PATCH" });
  }
  async deleteProject(projectId: string): Promise<void> { await this.json(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" }); }
  async browseDirectory(path?: string): Promise<DirectoryListing> { return this.json(`/api/fs/browse${path ? `?${new URLSearchParams({ path })}` : ""}`); }
  async makeDirectory(parentPath: string, name: string): Promise<{ name: string; path: string }> { return this.json("/api/fs/mkdir", jsonPost({ parentPath, name })); }
  async projectRepository(projectId: string): Promise<ProjectRepository> { return this.json(`/api/projects/${encodeURIComponent(projectId)}/repository`); }
  async createProjectSession(projectId: string, environment: "auto" | "local" | "worktree" = "auto", startingState: "default" | "working-tree" = "default"): Promise<SessionSummary> {
    return this.json(`/api/projects/${encodeURIComponent(projectId)}/sessions`, jsonPost({ environment, startingState: { type: startingState } }));
  }
  async bindSessionToProject(sessionId: string, projectId: string): Promise<SessionSummary> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/project`, jsonPost({ projectId })); }
  async sessionEnvironment(sessionId: string): Promise<{ sessionId: string; environment: SessionEnvironment | null }> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/environment`); }
  async handoffSession(sessionId: string, target: "local" | "worktree"): Promise<SessionSummary> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/handoff`, jsonPost({ target })); }
  async sessionChanges(sessionId: string): Promise<SessionChanges> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/changes`); }
  async sessionDiff(sessionId: string, path: string): Promise<string> {
    const result = await this.json<{ diff: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/diff?${new URLSearchParams({ path })}`);
    return result.diff;
  }
  async sessionContext(sessionId: string): Promise<SessionContextStatus> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/context`); }
  async compactSession(sessionId: string, instructions = "", reset = false): Promise<SessionContextStatus> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/compact`, jsonPost({ instructions, reset }), 120_000); }
  async integration(sessionId: string): Promise<IntegrationState> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/integration`); }
  async runIntegration(sessionId: string, action: "start" | "continue" | "abort"): Promise<IntegrationState> {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/integration${action === "start" ? "" : `/${action}`}`, { method: "POST" }, 120_000);
  }
  async commitWorkspace(sessionId: string, message: string): Promise<void> { await this.json(`/api/sessions/${encodeURIComponent(sessionId)}/git/commit`, jsonPost({ message }), 120_000); }
  async integrationFile(sessionId: string, path: string): Promise<IntegrationFile> { return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/integration/file?${new URLSearchParams({ path })}`); }
  async resolveIntegrationFile(sessionId: string, file: IntegrationFile, choice: "manual" | "ours" | "theirs" | "delete", content: string): Promise<IntegrationState> {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/integration/file`, jsonPut({ path: file.path, expectedHash: file.hash, choice, content }));
  }
  async saveModelsConfig(models: ModelProfile[]): Promise<void> { await this.json("/api/config/models", jsonPut({ models }), 120_000); }
  async createScheduledTask(input: ScheduledTaskInput): Promise<ScheduledTaskSummary> { return this.json("/api/scheduled-tasks", jsonPost(input)); }
  async updateScheduledTask(taskId: string, input: ScheduledTaskInput): Promise<ScheduledTaskSummary> { return this.json(`/api/scheduled-tasks/${encodeURIComponent(taskId)}`, jsonPut(input)); }
  async deleteScheduledTask(taskId: string): Promise<void> { await this.json(`/api/scheduled-tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" }); }
  async scheduledRunSession(taskId: string, runId: string): Promise<SessionState> { return this.json(`/api/scheduled-tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/session`); }
  async resumeScheduledApproval(taskId: string, runId: string, interruptId: string, action: "approve" | "deny" | "cancel" | "answer", scope: "once" | "run", answers?: import("../protocol/index.js").UserQuestionAnswers): Promise<unknown> {
    return this.json(`/api/scheduled-tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/resume`, jsonPost({ interruptId, action, scope, answers }), 120_000);
  }
  async createTeam(input: Record<string, unknown>): Promise<TeamSummary> { return this.json("/api/teams", jsonPost(input)); }
  async teamEvents(teamId: string, afterSeq = 0, taskId?: string, limit = 2000): Promise<Record<string, unknown>[]> {
    return this.json(`/api/teams/${encodeURIComponent(teamId)}/events?${new URLSearchParams({ afterSeq: String(afterSeq), limit: String(limit), ...(taskId ? { taskId } : {}) })}`);
  }
  async teamWorkspace(teamId: string): Promise<unknown> { return this.json(`/api/teams/${encodeURIComponent(teamId)}/workspace`); }
  async teamWorkspaceFile(teamId: string, path: string): Promise<unknown> { return this.json(`/api/teams/${encodeURIComponent(teamId)}/workspace/file?${new URLSearchParams({ path })}`); }
  async sendTeamMessage(teamId: string, recipientId: string, content: string): Promise<TeamSummary> {
    return this.json(`/api/teams/${encodeURIComponent(teamId)}/messages`, jsonPost({ senderId: "user", recipientId, messageType: "user_message", content }));
  }
  async resumeTeamApproval(teamId: string, approvalId: string, action: "approve" | "deny" | "cancel" | "answer", scope: "once" | "run", answers?: import("../protocol/index.js").UserQuestionAnswers): Promise<unknown> {
    return this.json(`/api/teams/${encodeURIComponent(teamId)}/approvals/${encodeURIComponent(approvalId)}/resume`, jsonPost({ action, scope, answers }), 120_000);
  }

  /** 工作台仅接受目录中声明的 API 路径；超时和错误解释与普通 CLI 请求一致。 */
  async workbenchRequest(path: string, method = "GET", body?: unknown): Promise<unknown> {
    if (!path.startsWith("/api/") || path.includes("#")) throw new Error("无效工作台路径");
    return this.json(path, {
      method,
      ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    }, method === "GET" ? 20_000 : 120_000);
  }

  async importSkill(name: string, bytes: Uint8Array): Promise<unknown> {
    const form = new FormData();
    form.set("file", new Blob([new Uint8Array(bytes)]), name);
    return this.json("/api/skills", { method: "POST", body: form }, 120_000);
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

function jsonPut(body: unknown): RequestInit {
  return {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
