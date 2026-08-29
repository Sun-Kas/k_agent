import React from "react";
import { Box, Text } from "ink";
import type { ScheduledTaskSummary } from "../../protocol/index.js";
import { TERMINAL_DESIGN } from "../design.js";
import { ScreenFrame } from "../panels/ScreenFrame.js";

export function AutomationScreen({ automations }: { automations: ScheduledTaskSummary[] }): React.ReactElement {
  return (
    <ScreenFrame title="AUTOMATIONS" count={`${automations.length} 个任务`}>
      <Box justifyContent="space-between">
        <Text color={TERMINAL_DESIGN.colors.muted}>任务</Text>
        <Text color={TERMINAL_DESIGN.colors.muted}>状态 · 下次执行</Text>
      </Box>
      {!automations.length
        ? <Text color={TERMINAL_DESIGN.colors.muted}>暂无定时任务</Text>
        : automations.map((task) => (
          <Box key={task.id} justifyContent="space-between">
            <Text wrap="truncate-end">
              {task.status === "paused" ? TERMINAL_DESIGN.symbols.stopped : TERMINAL_DESIGN.symbols.running}
              {" "}{task.name || task.id}
            </Text>
            <Text color={TERMINAL_DESIGN.colors.muted}>{task.status} · {task.nextRunAt ?? "—"}</Text>
          </Box>
        ))}
    </ScreenFrame>
  );
}
