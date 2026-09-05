import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import type { RuntimeChoice } from "../types.js";

/**
 * `/model` `/agent` `/permissions` 的单选列表。贴在 Composer 下面，不盖住输入框。
 */
export function OptionMenu({ title, items, selected, currentId, hint }: {
  title: string;
  items: RuntimeChoice[];
  selected: number;
  currentId: string;
  hint: string;
}): React.ReactElement {
  const safeSelected = Math.min(selected, Math.max(0, items.length - 1));
  return (
    <Box flexDirection="column" borderStyle={TERMINAL_DESIGN.borders.panel} borderColor={TERMINAL_DESIGN.colors.accent} paddingX={1}>
      <Text bold color={TERMINAL_DESIGN.colors.accent}>{title}</Text>
      {items.length === 0
        ? <Text color={TERMINAL_DESIGN.colors.muted}>当前没有可选项。</Text>
        : items.map((item, itemIndex) => (
          <Text key={item.id} inverse={itemIndex === safeSelected} dimColor={!item.enabled}>
            {itemIndex === safeSelected ? `${TERMINAL_DESIGN.symbols.pointer} ` : "  "}
            {item.id === currentId ? "●" : "○"} {item.name}
            {item.name === item.id ? "" : ` (${item.id})`}
            {item.note ? ` · ${item.note}` : ""}
          </Text>
        ))}
      <Text color={TERMINAL_DESIGN.colors.muted}>{hint}</Text>
    </Box>
  );
}
