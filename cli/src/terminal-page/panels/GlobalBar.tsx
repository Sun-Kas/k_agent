import React from "react";
import { Box, Text } from "ink";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";
import { HOME_MODES, isActiveSurface } from "../home-catalog.js";
import { workspaceIsShared } from "../project-home.js";

/**
 * REPL 顶栏只占一行：品牌、连接、当前会话。
 * 模式不再做成 Web 式 Tab，改由 Shift+Tab / 1–4 切换，脚注里显示当前位置。
 */
export function GlobalBar({ model }: { model: TerminalPageViewModel }): React.ReactElement {
  const mode = HOME_MODES.find((item) => isActiveSurface(model.surface, item.surface));
  const project = model.projects.find((item) => item.id === model.activeProjectId);
  const shared = workspaceIsShared(model.environment, model.sessions, model.activeSessionId);
  const status = model.connected
    ? `${TERMINAL_DESIGN.symbols.running} ${TERMINAL_DESIGN.copy.connected}`
    : `${TERMINAL_DESIGN.symbols.error} ${TERMINAL_DESIGN.copy.disconnected}`;
  return (
    <Box paddingX={1} justifyContent="space-between">
      <Text>
        <Text color={TERMINAL_DESIGN.colors.accent}>{TERMINAL_DESIGN.symbols.brand} </Text>
        <Text bold>{TERMINAL_DESIGN.copy.brand}</Text>
        <Text color={model.connected ? TERMINAL_DESIGN.colors.success : TERMINAL_DESIGN.colors.danger}>  {status}</Text>
        {mode ? <Text color={TERMINAL_DESIGN.colors.muted}>  {mode.title}</Text> : null}
        {model.surface === "projects" ? <Text color={TERMINAL_DESIGN.colors.muted}>  项目</Text> : null}
        {project ? <Text color={TERMINAL_DESIGN.colors.muted}>  {project.name}</Text> : null}
        {model.activeSessionTitle
          ? <Text color={TERMINAL_DESIGN.colors.muted}>  {model.activeSessionTitle}</Text>
          : null}
        {model.environment ? <Text color={TERMINAL_DESIGN.colors.muted}>  {model.environment.kind}{shared ? " · 共享" : ""} · {model.environment.branchRef || (model.environment.detached ? "detached" : "无分支")} · {model.environment.cwd}</Text> : null}
        {model.attachments?.length ? <Text color="yellow">  附件 {model.attachments.length}</Text> : null}
      </Text>
      <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">
        {model.runtime.modelId || model.runtime.agentKind}
      </Text>
    </Box>
  );
}
