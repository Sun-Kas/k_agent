import { appConfig } from "../config";
import type { ScheduledRun, ScheduledTask, ScheduledTaskInput } from "./types";
import type { SessionState } from "../types";

const url = (path: string) => `${appConfig.apiBaseUrl}${path}`;

async function errorMessage(response: Response, fallback: string) {
  try {
    const payload = await response.json() as { detail?: string | Array<{ msg?: string }> };
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) return payload.detail.map((item) => item.msg).filter(Boolean).join("；");
  } catch {
    // Preserve the HTTP fallback for non-JSON proxy errors.
  }
  return `${fallback}（${response.status}）`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url(path), init);
  } catch {
    throw new Error("无法连接 Access Layer，请确认本地服务已重启并运行在 3001 端口");
  }
  if (!response.ok) throw new Error(await errorMessage(response, "定时任务请求失败"));
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const listScheduledTasks = () => request<ScheduledTask[]>("/api/scheduled-tasks");
export const createScheduledTask = (input: ScheduledTaskInput) => request<ScheduledTask>("/api/scheduled-tasks", {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input)
});
export const updateScheduledTask = (id: string, input: ScheduledTaskInput) => request<ScheduledTask>(`/api/scheduled-tasks/${id}`, {
  method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input)
});
export const pauseScheduledTask = (id: string) => request<ScheduledTask>(`/api/scheduled-tasks/${id}/pause`, { method: "POST" });
export const resumeScheduledTask = (id: string) => request<ScheduledTask>(`/api/scheduled-tasks/${id}/resume`, { method: "POST" });
export const deleteScheduledTask = (id: string) => request<void>(`/api/scheduled-tasks/${id}`, { method: "DELETE" });
export const runScheduledTaskNow = (id: string) => request<ScheduledRun>(`/api/scheduled-tasks/${id}/run-now`, { method: "POST" });
export const listScheduledRuns = (id: string) => request<ScheduledRun[]>(`/api/scheduled-tasks/${id}/runs`);
export const getScheduledRunSession = (taskId: string, runId: string) => request<SessionState>(
  `/api/scheduled-tasks/${taskId}/runs/${runId}/session`
);
export const resumeScheduledRunApproval = (
  taskId: string,
  runId: string,
  interruptId: string,
  action: "approve" | "deny",
  scope: "once" | "run"
) => request<{ ok: boolean; status: string; runId: string }>(
  `/api/scheduled-tasks/${taskId}/runs/${runId}/resume`,
  {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ interruptId, action, scope })
  }
);
