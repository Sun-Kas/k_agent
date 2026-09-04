import type { AgUiEvent, ChatMessage } from "./types";

export function runHasVisibleActivity(events: AgUiEvent[]): boolean {
  return events.some((event) =>
    event.type === "TEXT_MESSAGE_START"
    || event.type === "TEXT_MESSAGE_CONTENT"
    || event.type === "REASONING_START"
    || event.type === "REASONING_MESSAGE_START"
    || event.type === "REASONING_MESSAGE_CONTENT"
    || event.type === "TOOL_CALL_START"
    || event.type === "TOOL_CALL_ARGS"
    || event.type === "TOOL_CALL_RESULT"
    || (event.type === "ACTIVITY_SNAPSHOT" && event.activityType === "approval")
    || (event.type === "CUSTOM" && (
      event.name === "approval_request"
      || event.name === "tool_output_delta"
    ))
  );
}

export function keepStoppedRunMessages(
  messages: ChatMessage[],
  runId: string,
  events: AgUiEvent[]
): ChatMessage[] {
  const hasVisibleActivity = runHasVisibleActivity(events);
  return messages.filter((message) => !(
    message.role === "assistant"
    && message.meta?.runId === runId
    && !message.content.trim()
    // Empty assistant rows are usually placeholders, but during streaming they
    // also host text/tool/thinking activities. Keep the host when stopping a run
    // so the current page matches the persisted replay without requiring refresh.
    && !hasVisibleActivity
  ));
}
