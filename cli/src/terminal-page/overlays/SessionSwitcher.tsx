import React, { useState } from "react";
import { Text, useInput, useStdout } from "ink";
import type { ProjectSummary, SessionSummary } from "../../protocol/index.js";
import { TERMINAL_DESIGN } from "../design.js";
import { deleteSessionWarning, sessionMeta, sessionsInProject } from "../project-home.js";
import type { TerminalPageAction } from "../types.js";
import { Overlay } from "./Overlay.js";

/**
 * 会话列表默认停在当前项目里。分支、移入和删除在同一列表完成，打开后直接回到对话。
 */
export function SessionSwitcher({ sessions, projects, activeSessionId, activeProjectId, running, onAction, onClose }: {
  sessions: SessionSummary[];
  projects: ProjectSummary[];
  activeSessionId?: string | undefined;
  activeProjectId?: string | undefined;
  running: boolean;
  onAction: (action: TerminalPageAction) => void;
  onClose: () => void;
}): React.ReactElement {
  const { stdout } = useStdout();
  const pageSize = Math.max(3, (stdout.rows ?? 24) - 10);
  const [scope, setScope] = useState<"project" | "all">(activeProjectId ? "project" : "all");
  const visible = scope === "project" && activeProjectId ? sessionsInProject(sessions, activeProjectId) : sessions;
  const initial = Math.max(0, visible.findIndex((session) => session.id === activeSessionId));
  const [selected, setSelected] = useState(initial);
  const [confirming, setConfirming] = useState(false);
  const current = visible[Math.min(selected, Math.max(0, visible.length - 1))];
  const busy = running && current?.id === activeSessionId;
  useInput((input, key) => {
    if (key.escape) {
      if (confirming) return setConfirming(false);
      return onClose();
    }
    if (key.tab && activeProjectId) return setScope((value) => value === "project" ? "all" : "project");
    if (!visible.length || !current) return;
    if (key.upArrow) return setSelected((value) => (value + visible.length - 1) % visible.length);
    if (key.downArrow) return setSelected((value) => (value + 1) % visible.length);
    if (confirming && key.return) {
      onAction({ type: "delete_session", sessionId: current.id });
      onClose();
      return;
    }
    if (key.return) {
      onAction({ type: "select_session", sessionId: current.id });
      onClose();
      return;
    }
    if (busy) return;
    if (input === "b") {
      onAction({ type: "fork_session", sessionId: current.id, environment: "same-directory" });
      onClose();
    }
    if (input === "i" && current.environment?.repositoryRoot) {
      onAction({ type: "fork_session", sessionId: current.id, environment: "worktree" });
      onClose();
    }
    if (input === "m" && !current.projectId && activeProjectId) {
      onAction({ type: "bind_session", sessionId: current.id, projectId: activeProjectId });
      onClose();
    }
    if (input === "d") setConfirming(true);
  });
  const start = Math.max(0, Math.min(selected - Math.floor(pageSize / 2), visible.length - pageSize));
  const projectName = projects.find((item) => item.id === activeProjectId)?.name;
  return (
    <Overlay title={scope === "project" && projectName ? projectName : "全部会话"}>
      {visible.length ? visible.slice(start, start + pageSize).map((session, index) => (
        <Text key={session.id} inverse={start + index === selected} wrap="truncate-end">{start + index === selected ? "›" : " "} {session.title || "未命名会话"} · {sessionMeta(session, projects)}</Text>
      )) : <Text color={TERMINAL_DESIGN.colors.muted}>{activeProjectId && scope === "project" ? "这个项目还没有会话" : "暂无 Session"}</Text>}
      {confirming && current ? <Text color={TERMINAL_DESIGN.colors.warning}>{deleteSessionWarning(current)} Enter 确认</Text> : null}
      <Text color={TERMINAL_DESIGN.colors.muted}>Enter 打开 · b 同目录分支 · i 隔离分支 · m 移入当前项目 · d 删除{activeProjectId ? " · Tab 切换范围" : ""} · Esc 关闭</Text>
    </Overlay>
  );
}
