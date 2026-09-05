import React from "react";
import { Box, Text } from "ink";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import type { TimelineItem } from "../../application/event-projector.js";
import { TERMINAL_DESIGN } from "../design.js";

export function ThinkingBlock({ item, expanded = false }: { item: Extract<TimelineItem, { kind: "thinking" }>; expanded?: boolean }): React.ReactElement {
  const symbol = item.status === "active" ? TERMINAL_DESIGN.symbols.running : TERMINAL_DESIGN.symbols.thinking;
  return (
    <Box flexDirection="column" paddingLeft={1}>
      <Text color={item.status === "active" ? TERMINAL_DESIGN.colors.accent : TERMINAL_DESIGN.colors.muted}>
        {symbol} {item.title}{item.status === "active" ? "" : ` · ${item.status}`}
      </Text>
      {expanded && item.content ? <Text color={TERMINAL_DESIGN.colors.muted} wrap="wrap">{sanitizeTerminalContent(item.content)}</Text> : null}
    </Box>
  );
}
