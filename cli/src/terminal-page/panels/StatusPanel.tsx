import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import type { SlashStatusPanel } from "../slash-catalog.js";

/**
 * 只读 slash 查询结果，贴在 Composer 下面。
 * 不占用 overlay，输入框保持可见；下一命令或开始对话时由页面清空。
 */
export function StatusPanel({ panel }: { panel: SlashStatusPanel }): React.ReactElement {
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text bold color={TERMINAL_DESIGN.colors.accent}>{panel.title}</Text>
      {panel.lines.map((line, index) => {
        const muted = line.startsWith("开关") || line.startsWith("切换") || line.startsWith("修改");
        return muted
          ? <Text key={`${index}:${line}`} color={TERMINAL_DESIGN.colors.muted}>{line}</Text>
          : <Text key={`${index}:${line}`}>{line}</Text>;
      })}
    </Box>
  );
}
