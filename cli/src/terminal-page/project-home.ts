import type { ProjectSummary, SessionEnvironment, SessionSummary } from "../protocol/index.js";

/**
 * 项目首页的纯数据。交互顺序对齐「先进入文件夹，再管理其中的会话」：
 * 置顶在前，其余按更新时间；会话列表只属于当前项目。
 */
export interface ProjectStartChoice {
  id: "local" | "worktree" | "working-tree";
  label: string;
  environment: "local" | "worktree";
  startingState: "default" | "working-tree";
}

export function orderProjects(projects: readonly ProjectSummary[], pinnedIds: readonly string[]): ProjectSummary[] {
  const rank = new Map(pinnedIds.map((id, index) => [id, index]));
  return [...projects].sort((left, right) => {
    const leftRank = rank.get(left.id) ?? Number.MAX_SAFE_INTEGER;
    const rightRank = rank.get(right.id) ?? Number.MAX_SAFE_INTEGER;
    if (leftRank !== rightRank) return leftRank - rightRank;
    return Date.parse(right.updatedAt) - Date.parse(left.updatedAt);
  });
}

export function sessionsInProject(sessions: readonly SessionSummary[], projectId: string): SessionSummary[] {
  return sessions
    .filter((session) => session.projectId === projectId)
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
}

export function startChoices(project: ProjectSummary): ProjectStartChoice[] {
  const local: ProjectStartChoice = {
    id: "local",
    label: "在项目目录中新建对话",
    environment: "local",
    startingState: "default",
  };
  if (!project.isGitRepository) return [local];
  return [
    local,
    { id: "worktree", label: "在独立 Worktree 中新建对话", environment: "worktree", startingState: "default" },
    { id: "working-tree", label: "包含当前改动，新建 Worktree", environment: "worktree", startingState: "working-tree" },
  ];
}

/** Local 目录由项目内会话共享；相同 cwd 的 Worktree 同样要标出来。 */
export function workspaceIsShared(
  environment: SessionEnvironment | null | undefined,
  sessions: readonly SessionSummary[],
  sessionId?: string,
): boolean {
  if (!environment) return false;
  if (environment.kind === "local") return true;
  return sessions.some((session) => session.id !== sessionId && session.environment?.cwd === environment.cwd);
}

export function deleteSessionWarning(session: SessionSummary): string {
  return session.projectId
    ? "历史记录将删除，项目和 Worktree 文件会保留。"
    : "历史记录和工作区文件将永久删除。";
}

export function sessionMeta(session: SessionSummary, projects: readonly ProjectSummary[]): string {
  const project = projects.find((item) => item.id === session.projectId);
  const environment = session.environment;
  const place = !environment
    ? "未绑定目录"
    : `${environment.kind === "worktree" ? "Worktree" : "Local"}${environment.branchRef ? ` ${environment.branchRef}` : environment.detached ? " detached" : ""}`;
  return `${session.messageCount} 条 · ${place}${project ? ` · ${project.name}` : ""}`;
}
