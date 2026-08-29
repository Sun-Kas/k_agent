import React from "react";
import { Box, Text } from "ink";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { TERMINAL_DESIGN } from "../design.js";

export function CodeBlock({ content, language = "text" }: { content: string; language?: string }): React.ReactElement {
  const lines = sanitizeTerminalContent(content).replace(/\n$/, "").split("\n");
  return (
    <Box flexDirection="column" borderStyle="single" borderColor={TERMINAL_DESIGN.colors.muted} paddingX={1}>
      <Text color={TERMINAL_DESIGN.colors.muted}>{language}</Text>
      {lines.map((line, index) => (
        <Text key={index} wrap="truncate-end">
          <Text color={TERMINAL_DESIGN.colors.muted}>{String(index + 1).padStart(2, " ")} </Text>
          {line || " "}
        </Text>
      ))}
    </Box>
  );
}
