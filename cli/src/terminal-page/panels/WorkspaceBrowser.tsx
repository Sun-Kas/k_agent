import React from "react";
import { Box, Text } from "ink";
import type { SessionWorkspaceFileContent, SessionWorkspaceListing } from "../../protocol/index.js";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { TERMINAL_DESIGN } from "../design.js";

export function WorkspaceBrowser({ listing, file }: {
  listing?: SessionWorkspaceListing | undefined;
  file?: SessionWorkspaceFileContent | undefined;
}): React.ReactElement {
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text bold>WORKSPACE</Text>
      {!listing ? <Text color={TERMINAL_DESIGN.colors.muted}>按 /workspace 从 Access Layer 读取会话工作区。</Text> : null}
      {listing && !file ? listing.files.slice(0, 20).map((entry) => (
        <Text key={entry.path} wrap="truncate-end">{entry.path}  <Text color={TERMINAL_DESIGN.colors.muted}>{entry.size} B</Text></Text>
      )) : null}
      {file ? (
        <Box flexDirection="column">
          <Text color={TERMINAL_DESIGN.colors.accent}>{file.path}{file.truncated ? " · 已截断" : ""}</Text>
          <Text wrap="wrap">{file.binary ? "[二进制文件不可预览]" : sanitizeTerminalContent(file.content)}</Text>
        </Box>
      ) : null}
    </Box>
  );
}
