import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import type { TerminalPageViewModel } from "../types.js";

/**
 * `/skill` 管理栏。贴在 Composer 下面；开关写入 Access Layer，与 Web 配置中心同一份目录。
 */
export function SkillMenu({ model, selected }: {
  model: TerminalPageViewModel;
  selected: number;
}): React.ReactElement {
  const skills = model.runtime.skills;
  const safeSelected = Math.min(selected, Math.max(0, skills.length - 1));
  return (
    <Box flexDirection="column" borderStyle={TERMINAL_DESIGN.borders.panel} borderColor={TERMINAL_DESIGN.colors.accent} paddingX={1}>
      <Text bold color={TERMINAL_DESIGN.colors.accent}>Skill</Text>
      {skills.length === 0
        ? <Text color={TERMINAL_DESIGN.colors.muted}>当前没有配置 Skill。</Text>
        : skills.map((skill, itemIndex) => (
          <Text key={skill.id} inverse={itemIndex === safeSelected}>
            {itemIndex === safeSelected ? `${TERMINAL_DESIGN.symbols.pointer} ` : "  "}
            {skill.enabled ? "● on " : "○ off"} {skill.name}
            {skill.name === skill.id ? "" : ` (${skill.id})`}
          </Text>
        ))}
      <Text color={TERMINAL_DESIGN.colors.muted}>
        {model.mcpBusy
          ? "正在写入 Access Layer…"
          : "↑↓ 选择   Space / Enter 开关   Esc 关闭"}
      </Text>
    </Box>
  );
}
