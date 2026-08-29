import React from "react";
import { Box, Text } from "ink";
import type { SessionSummary } from "../../protocol/index.js";
import { TERMINAL_DESIGN } from "../design.js";
import { relativeTime } from "../home-catalog.js";

export function SessionRail({ sessions, activeSessionId, focused }: {
  sessions: SessionSummary[];
  activeSessionId?: string | undefined;
  focused: boolean;
}): React.ReactElement {
  return (
    <Box
      flexDirection="column"
      width={TERMINAL_DESIGN.layout.sessionRailColumns}
      paddingX={1}
      borderStyle={TERMINAL_DESIGN.borders.panel}
      borderTop={false}
      borderBottom={false}
      borderLeft={false}
      borderColor={focused ? TERMINAL_DESIGN.colors.accent : TERMINAL_DESIGN.colors.muted}
    >
      <Text bold {...(focused ? { color: TERMINAL_DESIGN.colors.accent } : {})}>SESSIONS</Text>
      {sessions.length === 0
        ? <Text color={TERMINAL_DESIGN.colors.muted}>暂无会话</Text>
        : sessions.slice(0, TERMINAL_DESIGN.density.sessionVisibleRows).map((session) => {
          const active = session.id === activeSessionId;
          return (
            <Text key={session.id} inverse={active} wrap="truncate-end">
              {active ? TERMINAL_DESIGN.symbols.pointer : " "} {session.title || "未命名会话"}
            </Text>
          );
        })}
      {sessions[0] ? (
        <Text color={TERMINAL_DESIGN.colors.muted}>最近更新 {relativeTime(sessions[0].updatedAt)}</Text>
      ) : null}
    </Box>
  );
}
