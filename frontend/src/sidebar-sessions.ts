import type { SessionSummary } from "./types";

/** 最近列表以会话为中心；项目归属只参与筛选，不生成分组或额外的项目标签。 */
export function selectSidebarSessions(sessions: SessionSummary[], query: string, projectId: string | null): SessionSummary[] {
  const term = query.trim().toLocaleLowerCase();
  return sessions
    .filter((session) => (!projectId || session.projectId === projectId) && session.title.toLocaleLowerCase().includes(term))
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
}
