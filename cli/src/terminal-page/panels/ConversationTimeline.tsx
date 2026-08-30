import React from "react";
import { Box, Text } from "ink";
import type { TimelineItem } from "../../application/event-projector.js";
import { MarkdownBlock } from "../renderers/MarkdownBlock.js";
import { ThinkingBlock } from "../renderers/ThinkingBlock.js";
import { ToolBlock } from "../renderers/ToolBlock.js";
import { ErrorBlock } from "../renderers/ErrorBlock.js";
import { TERMINAL_DESIGN } from "../design.js";

export function ConversationTimeline({ items, selectedActivityId, expanded }: {
  items: TimelineItem[];
  selectedActivityId?: string | undefined;
  expanded: boolean;
}): React.ReactElement | null {
  if (items.length === 0) return null;
  return (
    <Box flexDirection="column">
      {items.map((item, index) => (
        <Box key={`${item.id}-${item.sequence}`} flexDirection="column" width="100%" marginTop={index === 0 ? 0 : TERMINAL_DESIGN.density.timelineGapRows}>
          <TimelineEntry item={item} expanded={expanded || item.id === selectedActivityId} />
        </Box>
      ))}
    </Box>
  );
}

export function TimelineEntry({ item, expanded }: {
  item: TimelineItem;
  expanded: boolean;
}): React.ReactElement {
  if (item.kind === "user") {
    const queued = item.id.startsWith("queued-");
    return (
      <Box width="100%">
        <Text color={queued ? TERMINAL_DESIGN.colors.muted : TERMINAL_DESIGN.colors.accent}>{TERMINAL_DESIGN.symbols.prompt} </Text>
        <Box flexDirection="column">
          <MarkdownBlock content={item.content} />
          {queued ? <Text color={TERMINAL_DESIGN.colors.muted}>排队发送</Text> : null}
        </Box>
      </Box>
    );
  }
  if (item.kind === "text") {
    return (
      <Box flexDirection="column" paddingLeft={2}>
        <MarkdownBlock content={item.content || "…"} />
      </Box>
    );
  }
  if (item.kind === "thinking") return <ThinkingBlock item={item} expanded={expanded} />;
  if (item.kind === "tool") return <ToolBlock item={item} expanded={expanded} />;
  if (item.kind === "approval") {
    return <Text color={TERMINAL_DESIGN.colors.warning}>{TERMINAL_DESIGN.symbols.approval} {item.approval.title} · {item.approval.status}</Text>;
  }
  return <ErrorBlock content={item.content} />;
}
