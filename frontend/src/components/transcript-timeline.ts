import type { AgUiEvent, ToolActivity } from "../types";

export type StaticTimelineActivity =
  | { type: "thinking"; id: string; title: string; detail: string }
  | { type: "tool"; id: string; tool: ToolActivity }
  | { type: "text"; id: string; content: string };

function toolResultFailed(content: string): boolean {
  try {
    const payload = JSON.parse(content) as Record<string, unknown>;
    return payload.ok === false || payload.success === false || payload.isError === true;
  } catch { return false; }
}

/**
 * 定时任务详情没有实时 React 状态可复用，因此直接按落盘 AG-UI 的到达顺序投影。
 * thinking、工具和正文都是硬边界，不能从 messages 按类型重新分组。
 */
export function timelineFromEvents(events: AgUiEvent[]): StaticTimelineActivity[] {
  const timeline: StaticTimelineActivity[] = [];
  const tools = new Map<string, ToolActivity>();
  const texts = new Map<string, Extract<StaticTimelineActivity, { type: "text" }>>();
  let runId = "scheduled";
  let activeThinking: Extract<StaticTimelineActivity, { type: "thinking" }> | null = null;
  for (const event of events) {
    if (event.type === "RUN_STARTED") runId = event.runId;
    if (event.type === "REASONING_START") {
      activeThinking = { type: "thinking", id: event.messageId, title: "思考过程", detail: "" };
      timeline.push(activeThinking);
    } else if (event.type === "REASONING_MESSAGE_START") {
      const raw = event.rawEvent && typeof event.rawEvent === "object" ? event.rawEvent as Record<string, unknown> : {};
      if (!activeThinking) {
        activeThinking = { type: "thinking", id: `reasoning-${event.messageId}`, title: "思考过程", detail: "" };
        timeline.push(activeThinking);
      }
      if (typeof raw.title === "string" && raw.title) activeThinking.title = raw.title;
    } else if (event.type === "REASONING_MESSAGE_CONTENT" && activeThinking) {
      activeThinking.detail += event.delta;
    } else if (event.type === "REASONING_MESSAGE_END" && activeThinking) {
      const raw = event.rawEvent && typeof event.rawEvent === "object" ? event.rawEvent as Record<string, unknown> : {};
      if (!activeThinking.detail && typeof raw.detail === "string") activeThinking.detail = raw.detail;
    } else if (event.type === "REASONING_END") {
      activeThinking = null;
    } else if (event.type === "TEXT_MESSAGE_START") {
      const text: Extract<StaticTimelineActivity, { type: "text" }> = { type: "text", id: event.messageId, content: "" };
      texts.set(event.messageId, text);
      timeline.push(text);
    } else if (event.type === "TEXT_MESSAGE_CONTENT") {
      let text = texts.get(event.messageId);
      if (!text) {
        text = { type: "text", id: event.messageId, content: "" };
        texts.set(event.messageId, text);
        timeline.push(text);
      }
      text.content += event.delta;
    } else if (event.type === "TOOL_CALL_START") {
      const tool = { id: event.toolCallId, turnId: runId, name: event.toolCallName, arguments: "", status: "preparing" as const, sequence: timeline.length };
      tools.set(event.toolCallId, tool);
      timeline.push({ type: "tool", id: event.toolCallId, tool });
      activeThinking = null;
    } else if (event.type === "TOOL_CALL_ARGS") { const tool = tools.get(event.toolCallId); if (tool) Object.assign(tool, { arguments: tool.arguments + event.delta, status: "running" }); }
    else if (event.type === "TOOL_CALL_END") { const tool = tools.get(event.toolCallId); if (tool) Object.assign(tool, { status: "waiting" }); }
    else if (event.type === "TOOL_CALL_RESULT") {
      const tool = tools.get(event.toolCallId);
      if (tool) Object.assign(tool, { result: event.content, status: toolResultFailed(event.content) ? "error" : "complete" });
    }
  }
  return timeline.filter((activity) => activity.type !== "text" || activity.content.length > 0);
}
