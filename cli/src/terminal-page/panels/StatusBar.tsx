import React from "react";
import { Box, Text } from "ink";
import type { TimelineState } from "../../application/event-projector.js";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";
import { runningActivityHint } from "../repl-view.js";

const RUN_STATUS_COPY: Record<TimelineState["runStatus"], { symbol: string; label: string; color?: string }> = {
  idle: { symbol: TERMINAL_DESIGN.symbols.waiting, label: "空闲" },
  running: { symbol: "⠋", label: TERMINAL_DESIGN.copy.running, color: TERMINAL_DESIGN.colors.success },
  waiting_input: { symbol: TERMINAL_DESIGN.symbols.approval, label: "等待输入", color: TERMINAL_DESIGN.colors.warning },
  complete: { symbol: TERMINAL_DESIGN.symbols.success, label: "已完成" },
  stopped: { symbol: TERMINAL_DESIGN.symbols.stopped, label: "已停止", color: TERMINAL_DESIGN.colors.warning },
  error: { symbol: TERMINAL_DESIGN.symbols.error, label: "失败", color: TERMINAL_DESIGN.colors.danger },
};

/**
 * 提示符下方的 REPL 脚注：能力摘要、运行态、少量快捷键。
 * 不单独占一块状态栏，避免对话和输入之间再隔出一条 Web 工具条。
 */
export function StatusBar({ model, queuedCount = 0 }: { model: TerminalPageViewModel; queuedCount?: number }): React.ReactElement {
  const status = RUN_STATUS_COPY[model.timeline.runStatus];
  const activity = model.timeline.runStatus === "running"
    ? runningActivityHint(model.timeline.items)
    : undefined;
  return (
    <Box paddingX={1} justifyContent="space-between">
      <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">
        {TERMINAL_DESIGN.copy.composerKeys}
        {queuedCount > 0 ? `   排队 ${queuedCount}` : ""}
        {model.notice ? `   ${model.notice}` : ""}
      </Text>
      <Text wrap="truncate-end">
        <Text color={TERMINAL_DESIGN.colors.muted}>
          mcp {model.runtime.mcpCount} · skills {model.runtime.skillCount}
        </Text>
        <Text {...(status.color ? { color: status.color } : { color: TERMINAL_DESIGN.colors.muted })}>
          {"  "}{status.symbol} {activity && activity !== status.label ? `${activity} · ` : ""}{status.label}
        </Text>
      </Text>
    </Box>
  );
}
