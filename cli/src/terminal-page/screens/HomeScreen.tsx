import React from "react";
import { Box, Text } from "ink";
import type { TerminalPageViewModel } from "../types.js";
import { TERMINAL_DESIGN } from "../design.js";
import { HOME_MODES, HOME_RECENT_SESSION_LIMIT, homePickItems, type HomeSessionItem } from "../home-catalog.js";

/**
 * 首页是“下一步做什么”的启动面板：先给可执行引导，再给模式与最近会话。
 * 这里不预置任何示例问题，避免用户把模板文案当成真实建议直接发给模型。
 */
export function HomeScreen({ model, selectedId }: {
  model: TerminalPageViewModel;
  selectedId: string;
}): React.ReactElement {
  const sessions = homePickItems(model).filter((item): item is HomeSessionItem => item.kind === "session");
  return (
    <Box flexDirection="column" flexGrow={1} paddingX={1}>
      <Box
        flexDirection="column"
        borderStyle={TERMINAL_DESIGN.borders.panel}
        borderColor={TERMINAL_DESIGN.colors.accent}
        paddingX={1}
      >
        <Text>
          <Text color={TERMINAL_DESIGN.colors.accent}>{TERMINAL_DESIGN.symbols.brand} </Text>
          <Text bold>{TERMINAL_DESIGN.copy.welcome}</Text>
          <Text color={TERMINAL_DESIGN.colors.muted}>  {TERMINAL_DESIGN.copy.tagline}</Text>
        </Text>
        <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">
          {model.runtime.endpoint} · {model.runtime.agentKind} · {model.runtime.modelId || "未选择模型"} · {model.runtime.permissionMode}
        </Text>
      </Box>

      <Box flexDirection="column" marginTop={1}>
        <Text bold color={TERMINAL_DESIGN.colors.accent}>{TERMINAL_DESIGN.copy.startGuideTitle}</Text>
        {TERMINAL_DESIGN.copy.startGuide.map((line) => (
          <Text key={line} color={TERMINAL_DESIGN.colors.muted}>  {TERMINAL_DESIGN.symbols.unread} {line}</Text>
        ))}
      </Box>

      <Box flexDirection="column" marginTop={1}>
        <Text bold color={TERMINAL_DESIGN.colors.accent}>模式</Text>
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
        <Box justifyContent="space-between">
          <Text bold color={TERMINAL_DESIGN.colors.accent}>最近会话</Text>
          <Text color={TERMINAL_DESIGN.colors.muted}>{TERMINAL_DESIGN.keys.sessionSwitcher} 全部</Text>
        </Box>
        {sessions.length === 0
          ? <Text color={TERMINAL_DESIGN.colors.muted}>  暂无会话，输入目标即可创建</Text>
          : sessions.slice(0, HOME_RECENT_SESSION_LIMIT).map((session) => (
            <PickRow
              key={session.id}
              selected={selectedId === session.id}
              badge={TERMINAL_DESIGN.symbols.waiting}
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
    <Box justifyContent="space-between">
      <Text inverse={selected}>
        {selected ? TERMINAL_DESIGN.symbols.pointer : " "} {badge}  {label}
      </Text>
      <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">{detail}</Text>
    </Box>
  );
}
