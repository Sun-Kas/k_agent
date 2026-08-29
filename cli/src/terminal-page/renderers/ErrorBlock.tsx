import React from "react";
import { Box, Text } from "ink";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { TERMINAL_DESIGN } from "../design.js";

export function ErrorBlock({ content }: { content: string }): React.ReactElement {
  return (
    <Box borderStyle="single" borderColor={TERMINAL_DESIGN.colors.danger} paddingX={1}>
      <Text color={TERMINAL_DESIGN.colors.danger}>{TERMINAL_DESIGN.symbols.error} {sanitizeTerminalContent(content)}</Text>
    </Box>
  );
}
