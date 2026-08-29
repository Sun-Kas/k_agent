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

export type HomePickItem = HomeModeItem | HomeSessionItem;

export const HOME_MODES: HomeModeItem[] = [
  { kind: "mode", id: "mode-home", key: "1", title: "工作", hint: "对话、工具与审批", surface: "home" },
  { kind: "mode", id: "mode-team", key: "2", title: "团队", hint: "Agent Team 与任务", surface: "team" },
  { kind: "mode", id: "mode-auto", key: "3", title: "自动", hint: "定时任务与执行记录", surface: "automation" },
  { kind: "mode", id: "mode-doctor", key: "4", title: "诊断", hint: "连接、模型与工具检查", surface: "doctor" },
];

export const HOME_RECENT_SESSION_LIMIT = 5;

export function homePickItems(model: TerminalPageViewModel): HomePickItem[] {
  const sessions: HomeSessionItem[] = model.sessions.slice(0, HOME_RECENT_SESSION_LIMIT).map((session) => ({
    kind: "session",
    id: `session-${session.id}`,
    title: session.title || "未命名会话",
    sessionId: session.id,
    subtitle: `${session.messageCount} 条 · ${relativeTime(session.updatedAt)}`,
  }));
  return [...HOME_MODES, ...sessions];
}

export function surfaceFromDigit(input: string): TerminalSurface | undefined {
  return HOME_MODES.find((item) => item.key === input)?.surface;
}

/** Shift+Tab 在一级模式之间循环；chat 属于“工作”模式，不算独立一级入口。 */
export function nextSurface(current: TerminalSurface, direction: 1 | -1): TerminalSurface {
  const index = HOME_MODES.findIndex((item) => isActiveSurface(current, item.surface));
  const base = index < 0 ? 0 : index;
  const next = HOME_MODES[(base + direction + HOME_MODES.length) % HOME_MODES.length];
  return next ? next.surface : "home";
}

export function isActiveSurface(current: TerminalSurface, mode: TerminalSurface): boolean {
  if (mode === "home") return current === "home" || current === "chat";
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
