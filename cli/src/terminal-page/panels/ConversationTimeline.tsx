import React from "react";
import { Box, Text } from "ink";
import type { TimelineItem } from "../../application/event-projector.js";
import { MarkdownBlock } from "../renderers/MarkdownBlock.js";
import { ThinkingBlock } from "../renderers/ThinkingBlock.js";
import { ToolBlock } from "../renderers/ToolBlock.js";
import { ErrorBlock } from "../renderers/ErrorBlock.js";
import { TERMINAL_DESIGN } from "../design.js";

export function ConversationTimeline({ items, selectedActivityId }: { items: TimelineItem[]; selectedActivityId?: string | undefined }): React.ReactElement {
  const visible = items.slice(-TERMINAL_DESIGN.density.timelineVisibleItems);
  return (
    <Box flexDirection="column" flexGrow={1} paddingX={1}>
      <Text bold color={TERMINAL_DESIGN.colors.muted}>CONVERSATION</Text>
      {visible.length === 0 ? (
        <Text color={TERMINAL_DESIGN.colors.muted}>输入目标开始新的运行。</Text>
      ) : visible.map((item) => (
        <Box key={`${item.id}-${item.sequence}`} flexDirection="column" marginTop={item.sequence === visible[0]?.sequence ? 0 : TERMINAL_DESIGN.density.timelineGapRows}>
          <TimelineEntry item={item} selected={item.id === selectedActivityId} />
        </Box>
      ))}
    </Box>
  );
}

function TimelineEntry({ item, selected }: { item: TimelineItem; selected: boolean }): React.ReactElement {
  if (item.kind === "user") {
    return (
      <Box flexDirection="column">
        <Text color={TERMINAL_DESIGN.colors.muted}>{TERMINAL_DESIGN.symbols.prompt} 你</Text>
        <MarkdownBlock content={item.content} />
      </Box>
    );
  }
  if (item.kind === "text") {
    return (
      <Box flexDirection="column">
        <Text color={TERMINAL_DESIGN.colors.accent}>{TERMINAL_DESIGN.symbols.brand} {TERMINAL_DESIGN.copy.brand}</Text>
        <MarkdownBlock content={item.content || "…"} />
      </Box>
    );
  }
  if (item.kind === "thinking") return <ThinkingBlock item={item} />;
  if (item.kind === "tool") return <ToolBlock item={item} expanded={selected} />;
  if (item.kind === "approval") {
    return <Text color={TERMINAL_DESIGN.colors.warning}>{TERMINAL_DESIGN.symbols.approval} {item.approval.title} · {item.approval.status}</Text>;
  }
  return <ErrorBlock content={item.content} />;
}
