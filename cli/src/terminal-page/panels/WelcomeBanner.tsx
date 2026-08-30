import React from "react";
import { Box, Text, useStdout } from "ink";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";
import { formatWorkspaceContext, workspaceContext } from "../workspace-context.js";

/**
 * REPL 开场：三行标 + 品名，边框包住内容，不拉满终端宽。
 */
export function WelcomeBanner({ model }: { model: TerminalPageViewModel }): React.ReactElement {
  const { stdout } = useStdout();
  const compact = (stdout.columns ?? 80) < TERMINAL_DESIGN.layout.compactMinColumns;
  const glyph = compact ? TERMINAL_DESIGN.copy.logoFallback : TERMINAL_DESIGN.copy.logo;
  const workspace = workspaceContext();

  return (
    <Box flexDirection="column" marginBottom={1}>
      <Box
        flexDirection="row"
        borderStyle={TERMINAL_DESIGN.borders.panel}
        borderColor={TERMINAL_DESIGN.colors.accent}
        paddingX={2}
        paddingY={1}
        alignSelf="flex-start"
      >
        <Box flexDirection="column" marginRight={2}>
          {glyph.map((line) => (
            <Text key={line} color={TERMINAL_DESIGN.colors.accent}>{line}</Text>
          ))}
        </Box>
        <Box flexDirection="column">
          <Text>
            <Text bold>{TERMINAL_DESIGN.copy.welcome}</Text>
            <Text color={TERMINAL_DESIGN.colors.muted}>  v{TERMINAL_DESIGN.copy.version}</Text>
          </Text>
          <Text color={TERMINAL_DESIGN.colors.muted}>{TERMINAL_DESIGN.copy.tagline}</Text>
          <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">
            {formatWorkspaceContext(workspace)} · {model.runtime.modelId || "未选择模型"}
          </Text>
          <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">
            {model.runtime.endpoint}
          </Text>
        </Box>
      </Box>
      <Box flexDirection="column" marginTop={1} paddingX={1}>
        {TERMINAL_DESIGN.copy.startGuide.map((line) => (
          <Text key={line} color={TERMINAL_DESIGN.colors.muted}>{line}</Text>
        ))}
      </Box>
    </Box>
  );
}
