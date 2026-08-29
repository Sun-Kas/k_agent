import React from "react";
import { Box, Text } from "ink";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import type { TimelineItem } from "../../application/event-projector.js";
import { TERMINAL_DESIGN } from "../design.js";

export function ToolBlock({ item, expanded = false }: { item: Extract<TimelineItem, { kind: "tool" }>; expanded?: boolean }): React.ReactElement {
  const color = item.status === "error"
    ? TERMINAL_DESIGN.colors.danger
    : item.status === "complete"
      ? TERMINAL_DESIGN.colors.success
      : TERMINAL_DESIGN.colors.accent;
  return (
    <Box flexDirection="column">
      <Text color={color}>{TERMINAL_DESIGN.symbols.tool} {sanitizeTerminalContent(item.name)} · {item.status}</Text>
      {expanded && item.arguments ? <Text color={TERMINAL_DESIGN.colors.muted}>参数  {sanitizeTerminalContent(item.arguments)}</Text> : null}
      {expanded && item.liveOutput ? <Text wrap="truncate-end">输出  {sanitizeTerminalContent(item.liveOutput)}</Text> : null}
      {expanded && item.result ? <Text wrap="truncate-end">结果  {sanitizeTerminalContent(item.result)}</Text> : null}
    </Box>
  );
}
