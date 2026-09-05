import React from "react";
import { Box, Text } from "ink";
import type { TimelineItem } from "../../application/event-projector.js";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { ToolBlock } from "../renderers/ToolBlock.js";
import { TERMINAL_DESIGN } from "../design.js";

export function ActivityInspector({ item }: { item?: TimelineItem | undefined }): React.ReactElement {
  return (
    <Box flexDirection="column" width={TERMINAL_DESIGN.layout.inspectorColumns} paddingX={1}>
      <Text color={TERMINAL_DESIGN.colors.muted}>inspect</Text>
      {!item ? <Text color={TERMINAL_DESIGN.colors.muted}>暂无运行活动</Text> : <InspectorItem item={item} />}
    </Box>
  );
}

function InspectorItem({ item }: { item: TimelineItem }): React.ReactElement {
  if (item.kind === "tool") return <ToolBlock item={item} expanded />;
  if (item.kind === "thinking") return <Text wrap="wrap">{sanitizeTerminalContent(item.content || item.title)}</Text>;
  if (item.kind === "approval") return <Text wrap="wrap">{sanitizeTerminalContent(item.approval.message)}</Text>;
  if (item.kind === "user" || item.kind === "text" || item.kind === "error") {
    return <Text wrap="wrap">{sanitizeTerminalContent(item.content)}</Text>;
  }
  return <Text color={TERMINAL_DESIGN.colors.muted}>无详情</Text>;
}
