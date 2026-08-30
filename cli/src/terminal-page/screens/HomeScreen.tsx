import React from "react";
import { Box, Text } from "ink";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";
import { HOME_MODES, HOME_RECENT_SESSION_LIMIT, homePickItems, type HomeSessionItem } from "../home-catalog.js";
import { WelcomeBanner } from "../panels/WelcomeBanner.js";

/**
 * 启动页先给 K Agent 欢迎框，再给最近会话。
 * 不预置示例问题；模式用 Shift+Tab / 1–4，不做成工作台导航。
 */
export function HomeScreen({ model, selectedId }: {
  model: TerminalPageViewModel;
  selectedId: string;
}): React.ReactElement {
  const sessions = homePickItems(model).filter((item): item is HomeSessionItem => item.kind === "session");
  return (
    <Box flexDirection="column" paddingX={1} paddingY={1}>
      <WelcomeBanner model={model} />

      <Box flexDirection="column" marginTop={1}>
        {HOME_MODES.map((item) => (
          <PickRow
            key={item.id}
            selected={selectedId === item.id}
            badge={item.key}
            label={item.title}
            detail={item.hint}
          />
        ))}
      </Box>

      <Box flexDirection="column" marginTop={1}>
        <Text color={TERMINAL_DESIGN.colors.muted}>最近会话 · {TERMINAL_DESIGN.keys.sessionSwitcher}</Text>
        {sessions.length === 0
          ? <Text color={TERMINAL_DESIGN.colors.muted}>暂无会话，输入目标即可创建</Text>
          : sessions.slice(0, HOME_RECENT_SESSION_LIMIT).map((session) => (
            <PickRow
              key={session.id}
              selected={selectedId === session.id}
              badge=""
              label={session.title}
              detail={session.subtitle}
            />
          ))}
      </Box>
    </Box>
  );
}

function PickRow({ selected, badge, label, detail }: {
  selected: boolean;
  badge: string;
  label: string;
  detail: string;
}): React.ReactElement {
  return (
    <Box>
      <Text inverse={selected}>
        {selected ? TERMINAL_DESIGN.symbols.pointer : " "} {badge ? `${badge} ` : ""}{label}
      </Text>
      <Text color={TERMINAL_DESIGN.colors.muted}>  {detail}</Text>
    </Box>
  );
}
