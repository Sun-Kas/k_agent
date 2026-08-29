import React from "react";
import { Box, Text } from "ink";
import type { TeamSummary } from "../../protocol/index.js";
import { TERMINAL_DESIGN } from "../design.js";
import { ScreenFrame } from "../panels/ScreenFrame.js";

export function TeamScreen({ teams }: { teams: TeamSummary[] }): React.ReactElement {
  return (
    <ScreenFrame title="AGENT TEAM" count={`${teams.length} 个团队`}>
      {!teams.length
        ? <Text color={TERMINAL_DESIGN.colors.muted}>暂无 Team。CLI 只做公开 API 客户端，不在本地创建第二套调度器。</Text>
        : teams.map((team) => (
          <Box key={team.id} justifyContent="space-between">
            <Text>
              {team.status === "running" ? TERMINAL_DESIGN.symbols.running : TERMINAL_DESIGN.symbols.waiting}
              {" "}{team.name ?? team.id}
            </Text>
            <Text color={TERMINAL_DESIGN.colors.muted}>{team.status ?? "unknown"}</Text>
          </Box>
        ))}
    </ScreenFrame>
  );
}
