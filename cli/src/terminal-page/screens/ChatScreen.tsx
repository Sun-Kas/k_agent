import React, { useMemo } from "react";
import { Box } from "ink";
import { ConversationTimeline } from "../panels/ConversationTimeline.js";
import { WelcomeBanner } from "../panels/WelcomeBanner.js";
import { ActivityInspector } from "../panels/ActivityInspector.js";
import { WorkspaceBrowser } from "../panels/WorkspaceBrowser.js";
import type { TerminalFocus, TerminalPageViewModel } from "../types.js";
import { selectedTimelineItem } from "../types.js";
import type { TerminalLayout } from "../design.js";
import { splitSettledTimeline } from "../repl-view.js";

/**
 * 对话是一段贴在提示符上方的 REPL 日志。
 * 已完成轮次由父层写入终端滚动区；这里只画仍在变化的条目。
 */
export function ChatScreen({ model, layout: _layout, focus: _focus, showInspector, pendingPrompts = [] }: {
  model: TerminalPageViewModel;
  layout: TerminalLayout;
  focus: TerminalFocus;
  showInspector: boolean;
  pendingPrompts?: string[];
}): React.ReactElement {
  const liveItems = useMemo(() => {
    const queued = pendingPrompts.map((content, index) => ({
      kind: "user" as const,
      id: `queued-${index}`,
      content,
      sequence: 1_000_000 + index,
    }));
    return [...splitSettledTimeline(model.timeline.items).live, ...queued];
  }, [model.timeline.items, pendingPrompts]);

  const empty = model.timeline.items.length === 0 && pendingPrompts.length === 0;
  return (
    <Box flexDirection="column" paddingX={1}>
      {empty ? <WelcomeBanner model={model} /> : <ConversationTimeline items={liveItems} selectedActivityId={model.selectedActivityId} expanded={showInspector} />}
      {showInspector
        ? (model.workspace || model.workspaceFile
          ? <WorkspaceBrowser listing={model.workspace} file={model.workspaceFile} />
          : <ActivityInspector item={selectedTimelineItem(model)} />)
        : null}
    </Box>
  );
}
