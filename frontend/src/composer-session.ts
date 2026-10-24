import type { SessionSummary } from "./types";

/** 先落库项目会话再提交 run，避免只显示项目标签、实际仍使用通用工作区。 */
export async function resolveComposerSession(sessionId: string | null, projectId: string | null, createProjectSession: (projectId: string) => Promise<SessionSummary>, createId: () => string): Promise<{ id: string; created?: SessionSummary }> {
  if (sessionId) return { id: sessionId };
  if (!projectId) return { id: createId() };
  const created = await createProjectSession(projectId);
  return { id: created.id, created };
}
