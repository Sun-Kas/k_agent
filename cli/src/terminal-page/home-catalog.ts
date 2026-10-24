import type { TerminalPageViewModel, TerminalSurface } from "./types.js";

export interface HomeModeItem {
  kind: "mode";
  id: string;
  key: string;
  title: string;
  hint: string;
  surface: TerminalSurface;
}

export interface HomeSessionItem {
  kind: "session";
  id: string;
  title: string;
  sessionId: string;
  subtitle: string;
}

export interface HomeProjectItem {
  kind: "project";
  id: string;
  projectId: string;
  title: string;
  subtitle: string;
}

export type HomePickItem = HomeModeItem | HomeSessionItem | HomeProjectItem;

/** 与网页侧栏功能入口一致：项目、技能、连接器、定时任务。 */
export const HOME_MODES: HomeModeItem[] = [
  { kind: "mode", id: "mode-projects", key: "1", title: "项目", hint: "项目目录与其中的会话", surface: "projects" },
  { kind: "mode", id: "mode-skills", key: "2", title: "技能", hint: "技能广场", surface: "skills" },
  { kind: "mode", id: "mode-connectors", key: "3", title: "连接器", hint: "MCP 广场", surface: "connectors" },
  { kind: "mode", id: "mode-auto", key: "4", title: "定时任务", hint: "计划与执行记录", surface: "automation" },
];

export const HOME_RECENT_SESSION_LIMIT = 5;

export function homePickItems(model: TerminalPageViewModel): HomePickItem[] {
  const projects: HomeProjectItem[] = model.projects.slice(0, 3).map((project) => ({
    kind: "project",
    id: `project-${project.id}`,
    projectId: project.id,
    title: project.name,
    subtitle: `${project.sessionCount} 个会话 · ${project.rootPath}`,
  }));
  const sessions: HomeSessionItem[] = model.sessions.slice(0, HOME_RECENT_SESSION_LIMIT).map((session) => ({
    kind: "session",
    id: `session-${session.id}`,
    title: session.title || "未命名会话",
    sessionId: session.id,
    subtitle: `${session.messageCount} 条 · ${relativeTime(session.updatedAt)}`,
  }));
  return [...HOME_MODES, ...projects, ...sessions];
}

export function surfaceFromDigit(input: string): TerminalSurface | undefined {
  return HOME_MODES.find((item) => item.key === input)?.surface;
}

/** Shift+Tab 在侧栏四个入口之间循环。对话页不属于这四项。 */
export function nextSurface(current: TerminalSurface, direction: 1 | -1): TerminalSurface {
  const index = HOME_MODES.findIndex((item) => isActiveSurface(current, item.surface));
  const base = index < 0 ? 0 : index;
  const next = HOME_MODES[(base + direction + HOME_MODES.length) % HOME_MODES.length];
  return next ? next.surface : "projects";
}

export function isActiveSurface(current: TerminalSurface, mode: TerminalSurface): boolean {
  return current === mode;
}

export function relativeTime(value: string): string {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "—";
  const minutes = Math.max(0, Math.round((Date.now() - timestamp) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}
