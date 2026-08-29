import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import type { SlashCommandItem } from "../slash-catalog.js";
import type { TerminalPageViewModel } from "../types.js";
import { CommandList } from "./CommandList.js";

/**
 * `/` 命令选择栏。
 *
 * 只做展示与高亮：选中项、过滤结果和执行动作都由页面根组件持有，
 * 避免选择栏自行触发命令而绕过统一的 slash 解析入口。
 */
export function SlashMenu({ items, selected, query, model }: {
  items: SlashCommandItem[];
  selected: number;
  query: string;
  model: TerminalPageViewModel;
}): React.ReactElement {
  return (
    <Box flexDirection="column" borderStyle={TERMINAL_DESIGN.borders.panel} borderColor={TERMINAL_DESIGN.colors.accent} paddingX={1}>
      <CommandList items={items} selected={selected} model={model} capacity={TERMINAL_DESIGN.layout.slashMenuVisibleRows} />
      <Text color={TERMINAL_DESIGN.colors.muted}>
        {query ? `匹配 “/${query}” · ` : ""}{TERMINAL_DESIGN.copy.slashKeys}
      </Text>
    </Box>
  );
}
