import React, { useMemo, useState } from "react";
import { Box, Text, useInput } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import { filterSlashCommands, slashCommandAvailability } from "../slash-catalog.js";
import type { TerminalPageViewModel } from "../types.js";
import { CommandList } from "../panels/CommandList.js";
import { Overlay } from "./Overlay.js";

/**
 * Ctrl+K 命令面板。
 *
 * 它和 `/` 选择栏消费同一份命令目录，区别只在于不依赖输入框：Run 正在进行、
 * 连接断开或草稿里已有正文时 `/` 无法展开，此时仍需要一个能执行 stop / cancel /
 * quit 的入口。执行动作统一交给页面根组件的 slash 提交路径，不在这里直接派发。
 */
export function CommandPalette({ model, onRun, onClose }: {
  model: TerminalPageViewModel;
  /** 只上报“用户选择了哪个命令”，参数补全与派发仍由根组件完成。 */
  onRun: (command: string, takesArguments: boolean) => void;
  onClose: () => void;
}): React.ReactElement {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const items = useMemo(() => filterSlashCommands(query), [query]);
  const index = Math.min(selected, Math.max(0, items.length - 1));
  const current = items[index];

  useInput((input, key) => {
    if (key.escape) return onClose();
    if (key.upArrow) return setSelected(() => (index + items.length - 1) % Math.max(1, items.length));
    if (key.downArrow) return setSelected(() => (index + 1) % Math.max(1, items.length));
    if (key.return) {
      if (!current) return;
      // 不可用命令保留在列表里但拒绝执行，避免用户以为动作已经生效。
      if (!slashCommandAvailability(current, model).enabled) return;
      onRun(current.name, current.takesArguments === true);
      return;
    }
    if (key.backspace || key.delete) {
      setQuery((value) => Array.from(value).slice(0, -1).join(""));
      setSelected(0);
      return;
    }
    if (!key.ctrl && !key.meta && !key.tab && input) {
      setQuery((value) => `${value}${input}`);
      setSelected(0);
    }
  });

  return (
    <Overlay title="命令">
      {/* 搜索行不使用候选指针符号，避免和列表高亮行的 ❯ 混淆。 */}
      <Box marginBottom={1}>
        <Text>
          <Text color={TERMINAL_DESIGN.colors.muted}>搜索  </Text>
          <Text color={TERMINAL_DESIGN.colors.accent}>/</Text>
          {query}
          <Text inverse> </Text>
        </Text>
      </Box>
      {items.length === 0
        ? <Text color={TERMINAL_DESIGN.colors.muted}>没有匹配的命令</Text>
        : <CommandList items={items} selected={index} model={model} capacity={TERMINAL_DESIGN.layout.paletteVisibleRows} />}
      <Text color={TERMINAL_DESIGN.colors.muted}>{TERMINAL_DESIGN.copy.paletteKeys}</Text>
    </Overlay>
  );
}
