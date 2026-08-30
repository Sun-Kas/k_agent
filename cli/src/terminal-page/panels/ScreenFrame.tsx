import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";

/**
 * 非对话页面的统一外框。
 *
 * 这些页面没有输入框，因此必须自己提示如何离开当前模式，
 * 否则用户在 Team / Automation / Doctor 里会找不到返回路径。
 */
export function ScreenFrame({ title, count, hint, children }: {
  title: string;
  count?: string;
  hint?: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <Box flexDirection="column" paddingX={1}>
      <Box justifyContent="space-between">
        <Text bold color={TERMINAL_DESIGN.colors.accent}>{title}</Text>
        {count ? <Text color={TERMINAL_DESIGN.colors.muted}>{count}</Text> : null}
      </Box>
      <Box flexDirection="column" marginTop={1}>{children}</Box>
      <Text color={TERMINAL_DESIGN.colors.muted}>
        {hint ? `${hint}   ` : ""}{TERMINAL_DESIGN.keys.modeSwitch} 切换模式   / 命令   /help 帮助
      </Text>
    </Box>
  );
}
