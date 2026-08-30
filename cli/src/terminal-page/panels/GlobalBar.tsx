import React from "react";
import { Box, Text } from "ink";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";
import { HOME_MODES, isActiveSurface } from "../home-catalog.js";

/**
 * REPL 顶栏只占一行：品牌、连接、当前会话。
 * 模式不再做成 Web 式 Tab，改由 Shift+Tab / 1–4 切换，脚注里显示当前位置。
 */
export function GlobalBar({ model }: { model: TerminalPageViewModel }): React.ReactElement {
  const mode = HOME_MODES.find((item) => isActiveSurface(model.surface, item.surface));
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
        {model.activeSessionTitle
          ? <Text color={TERMINAL_DESIGN.colors.muted}>  {model.activeSessionTitle}</Text>
          : null}
      </Text>
      <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">
        {model.runtime.modelId || model.runtime.agentKind}
      </Text>
    </Box>
  );
}
