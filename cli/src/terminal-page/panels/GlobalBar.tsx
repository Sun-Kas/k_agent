import React from "react";
import { Box, Text } from "ink";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";
import { HOME_MODES, isActiveSurface } from "../home-catalog.js";

/**
 * 顶部只承担两件事：当前连接与运行时身份，以及一级模式位置。
 * 状态必须同时用符号和文字表达，`--plain` 或无颜色终端下也能区分连接与断开。
 */
export function GlobalBar({ model }: { model: TerminalPageViewModel }): React.ReactElement {
  const status = model.connected
    ? `${TERMINAL_DESIGN.symbols.running} ${TERMINAL_DESIGN.copy.connected}`
    : `${TERMINAL_DESIGN.symbols.error} ${TERMINAL_DESIGN.copy.disconnected}`;
  return (
    <Box flexDirection="column" paddingX={1}>
      <Box justifyContent="space-between">
        <Text>
          <Text color={TERMINAL_DESIGN.colors.accent}>{TERMINAL_DESIGN.symbols.brand} </Text>
          <Text bold>{TERMINAL_DESIGN.copy.brand}</Text>
          <Text color={model.connected ? TERMINAL_DESIGN.colors.success : TERMINAL_DESIGN.colors.danger}>  {status}</Text>
          {model.activeSessionTitle
            ? <Text color={TERMINAL_DESIGN.colors.muted}>  {model.activeSessionTitle}</Text>
            : null}
        </Text>
        <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">
          {model.runtime.agentKind} · {model.runtime.modelId || "未选择模型"} · {model.runtime.permissionMode}
        </Text>
      </Box>
      <Box justifyContent="space-between">
        <Box>
          {HOME_MODES.map((item, index) => (
            <Box key={item.id}>
              {index > 0 ? <Text color={TERMINAL_DESIGN.colors.muted}> </Text> : null}
              <ModeTab
                label={`${item.key} ${item.title}`}
                active={isActiveSurface(model.surface, item.surface)}
              />
            </Box>
          ))}
        </Box>
        <Text color={TERMINAL_DESIGN.colors.muted}>{TERMINAL_DESIGN.keys.modeSwitch} 切换模式</Text>
      </Box>
    </Box>
  );
}

function ModeTab({ label, active }: { label: string; active: boolean }): React.ReactElement {
  if (active) return <Text inverse bold> {label} </Text>;
  return <Text color={TERMINAL_DESIGN.colors.muted}> {label} </Text>;
}
