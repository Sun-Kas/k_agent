import React, { useState } from "react";
import { Text, useInput } from "ink";
import type { SessionSummary } from "../../protocol/index.js";
import { TERMINAL_DESIGN } from "../design.js";
import { Overlay } from "./Overlay.js";

export function SessionSwitcher({ sessions, activeSessionId, onSelect, onClose }: {
  sessions: SessionSummary[];
  activeSessionId?: string | undefined;
  onSelect: (sessionId: string) => void;
  onClose: () => void;
}): React.ReactElement {
  const initial = Math.max(0, sessions.findIndex((session) => session.id === activeSessionId));
  const [selected, setSelected] = useState(initial);
  useInput((_input, key) => {
    if (key.escape) return onClose();
    if (!sessions.length) return;
    if (key.upArrow) return setSelected((value) => (value + sessions.length - 1) % sessions.length);
    if (key.downArrow) return setSelected((value) => (value + 1) % sessions.length);
    if (key.return) {
      onSelect(sessions[selected]!.id);
      onClose();
    }
  });
  return (
    <Overlay title="SESSIONS">
      {sessions.length ? sessions.slice(0, 12).map((session, index) => (
        <Text key={session.id} inverse={index === selected}>{index === selected ? "›" : " "} {session.title || "未命名会话"}</Text>
      )) : <Text color={TERMINAL_DESIGN.colors.muted}>暂无 Session</Text>}
      <Text color={TERMINAL_DESIGN.colors.muted}>↑↓ 选择 · Enter 打开 · Esc 关闭</Text>
    </Overlay>
  );
}
