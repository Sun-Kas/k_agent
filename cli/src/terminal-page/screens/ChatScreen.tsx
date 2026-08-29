import React from "react";
import { Box } from "ink";
import { ConversationTimeline } from "../panels/ConversationTimeline.js";
import { SessionRail } from "../panels/SessionRail.js";
import { ActivityInspector } from "../panels/ActivityInspector.js";
import { WorkspaceBrowser } from "../panels/WorkspaceBrowser.js";
import type { TerminalFocus, TerminalPageViewModel } from "../types.js";
import { selectedTimelineItem } from "../types.js";
import type { TerminalLayout } from "../design.js";

export function ChatScreen({ model, layout, focus, showInspector }: {
  model: TerminalPageViewModel;
  layout: TerminalLayout;
  focus: TerminalFocus;
  showInspector: boolean;
}): React.ReactElement {
  const showSessions = layout === "wide" || layout === "standard";
  const stableInspector = layout === "wide";
  return (
    <Box flexDirection="row" flexGrow={1}>
      {showSessions ? <SessionRail sessions={model.sessions} activeSessionId={model.activeSessionId} focused={focus === "session-rail"} /> : null}
      <ConversationTimeline items={model.timeline.items} selectedActivityId={model.selectedActivityId} />
      {stableInspector || showInspector ? (
        model.workspace || model.workspaceFile
          ? <WorkspaceBrowser listing={model.workspace} file={model.workspaceFile} />
          : <ActivityInspector item={selectedTimelineItem(model)} />
      ) : null}
    </Box>
  );
}
