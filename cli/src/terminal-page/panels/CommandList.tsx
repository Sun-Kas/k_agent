import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import { slashCommandAnnotation, slashCommandAvailability, type SlashCommandItem } from "../slash-catalog.js";
import type { TerminalPageViewModel } from "../types.js";

/**
 * `/` 选择栏与 Ctrl+K 命令面板共用的候选列表。
 *
 * 两处入口消费同一份目录和同一套渲染规则，避免命令表在两边各自演化后不同步。
 * 不可用命令仍然显示并注明原因，不能因为暂时不能执行就悄悄消失。
 */
export function CommandList({ items, selected, model, capacity }: {
  items: SlashCommandItem[];
  selected: number;
  model: TerminalPageViewModel;
  capacity: number;
}): React.ReactElement {
  // 选中项始终留在可视窗口内，长列表滚动时不会把高亮行推出边框。
  const start = Math.min(Math.max(0, selected - capacity + 1), Math.max(0, items.length - capacity));
  const visible = items.slice(start, start + capacity);
  const hidden = items.length - (start + visible.length);
  return (
    <>
      {visible.map((item, index) => {
        const active = start + index === selected;
        const { enabled } = slashCommandAvailability(item, model);
        return (
          <Box key={item.name} justifyContent="space-between">
            <Text {...(active && enabled ? {} : { color: TERMINAL_DESIGN.colors.muted })} inverse={active}>
              {active ? TERMINAL_DESIGN.symbols.pointer : " "} /{item.name}
              {item.takesArguments ? " …" : ""}
            </Text>
            <Text
              color={enabled ? TERMINAL_DESIGN.colors.muted : TERMINAL_DESIGN.colors.warning}
              wrap="truncate-end"
            >
              {slashCommandAnnotation(item, model)}
            </Text>
          </Box>
        );
      })}
      {hidden > 0 ? <Text color={TERMINAL_DESIGN.colors.muted}>  还有 {hidden} 个命令</Text> : null}
    </>
  );
}
