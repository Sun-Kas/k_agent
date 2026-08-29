import React from "react";
import { Box, Text } from "ink";
import type { TimelineState } from "../../application/event-projector.js";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";

const RUN_STATUS_COPY: Record<TimelineState["runStatus"], { symbol: string; label: string; color?: string }> = {
  idle: { symbol: TERMINAL_DESIGN.symbols.waiting, label: "空闲" },
  running: { symbol: TERMINAL_DESIGN.symbols.running, label: TERMINAL_DESIGN.copy.running, color: TERMINAL_DESIGN.colors.success },
  waiting_input: { symbol: TERMINAL_DESIGN.symbols.approval, label: "等待输入", color: TERMINAL_DESIGN.colors.warning },
  complete: { symbol: TERMINAL_DESIGN.symbols.success, label: "已完成" },
  stopped: { symbol: TERMINAL_DESIGN.symbols.stopped, label: "已停止", color: TERMINAL_DESIGN.colors.warning },
  error: { symbol: TERMINAL_DESIGN.symbols.error, label: "失败", color: TERMINAL_DESIGN.colors.danger },
};

/**
 * 底部状态行同时展示能力摘要和运行终态。
 * 运行状态不能只靠颜色区分，因此每种状态都带符号和中文标签。
 */
export function StatusBar({ model }: { model: TerminalPageViewModel }): React.ReactElement {
  const status = RUN_STATUS_COPY[model.timeline.runStatus];
  return (
    <Box justifyContent="space-between" paddingX={1}>
      <Text color={TERMINAL_DESIGN.colors.muted}>
        MCP {model.runtime.mcpCount} · Skills {model.runtime.skillCount} · Reasoning {model.runtime.reasoningEffort}
      </Text>
      <Text wrap="truncate-end">
        <Text {...(status.color ? { color: status.color } : { color: TERMINAL_DESIGN.colors.muted })}>
          {status.symbol} {status.label}
        </Text>
        {model.notice ? <Text color={TERMINAL_DESIGN.colors.muted}> · {model.notice}</Text> : null}
      </Text>
    </Box>
  );
}
