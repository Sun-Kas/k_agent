import { useEffect, useMemo, useState } from "react";

import type { AgUiEvent, ChatMessage, SessionState, ToolActivity } from "../types";
import { MarkdownContent } from "./MarkdownContent";

export function groupDisplayMessages(messages: ChatMessage[]): ChatMessage[] {
  const grouped: ChatMessage[] = [];
  for (const message of messages) {
    // Tool/system records feed model context and the activity timeline, not chat bubbles.
    if (message.role !== "user" && message.role !== "assistant") continue;
    const runId = message.role === "assistant" ? message.meta?.runId : undefined;
    const previous = grouped[grouped.length - 1];
    if (runId && previous?.role === "assistant" && previous.meta?.runId === runId) {
      const separator = previous.content.trim() && message.content.trim() ? "\n\n" : "";
      grouped[grouped.length - 1] = { ...previous, content: `${previous.content}${separator}${message.content}` };
    } else {
      grouped.push(message);
    }
  }
  return grouped;
}

export function toolResultFailed(content: string): boolean {
  try {
    const payload = JSON.parse(content) as Record<string, unknown>;
    return payload.ok === false || payload.success === false || payload.isError === true;
  } catch { return false; }
}

/** Read-only sessions reuse chat's bubbles, Markdown and collapsed tool presentation. */
export function StaticConversationTranscript({ session }: { session: SessionState }) {
  const messages = useMemo(() => groupDisplayMessages(session.messages), [session.messages]);
  const tools = useMemo(() => toolsFromEvents(session.events ?? []), [session.events]);
  return <div className="scheduled-transcript">{messages.map((message, index) =>
    <article className={`message-row ${message.role}`} key={message.id}>
      <div className="avatar">{message.role === "user" ? "你" : "K"}</div>
      <div className="message-body">
        {message.role === "assistant" && index === messages.length - 1 && tools.map((tool) => <InlineToolActivity tool={tool} key={tool.id} />)}
        {message.role === "assistant"
          ? <div className="assistant-output"><MarkdownContent content={message.content} /></div>
          : <div className="bubble"><MarkdownContent content={message.content} /></div>}
      </div>
    </article>)}</div>;
}

export function InlineToolActivity({ tool }: { tool: ToolActivity }) {
  const [open, setOpen] = useState(tool.status !== "complete");
  useEffect(() => { if (tool.status === "complete") setOpen(false); }, [tool.status]);
  return <section className={`inline-tool ${tool.status} ${open ? "open" : ""}`}>
    <button type="button" className="inline-tool-summary" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span className="inline-tool-icon" aria-hidden="true">⌁</span><strong>调用工具</strong><code>{tool.name}</code>
      <b>{tool.status === "complete" ? "已完成" : tool.status === "error" ? "失败" : "运行中"}</b><i aria-hidden="true">⌃</i>
    </button>
    {open && (tool.arguments || tool.result) && <div className="inline-tool-detail">
      {tool.arguments && <code>{tool.arguments}</code>}{tool.result && <p>{tool.result}</p>}
    </div>}
  </section>;
}

function toolsFromEvents(events: AgUiEvent[]): ToolActivity[] {
  const tools = new Map<string, ToolActivity>();
  let runId = "scheduled"; let sequence = 0;
  for (const event of events) {
    if (event.type === "RUN_STARTED") runId = event.runId;
    if (event.type === "TOOL_CALL_START") tools.set(event.toolCallId, { id: event.toolCallId, turnId: runId, name: event.toolCallName, arguments: "", status: "preparing", sequence: sequence++ });
    else if (event.type === "TOOL_CALL_ARGS") { const tool = tools.get(event.toolCallId); if (tool) tools.set(event.toolCallId, { ...tool, arguments: tool.arguments + event.delta, status: "running" }); }
    else if (event.type === "TOOL_CALL_END") { const tool = tools.get(event.toolCallId); if (tool) tools.set(event.toolCallId, { ...tool, status: "waiting" }); }
    else if (event.type === "TOOL_CALL_RESULT") { const tool = tools.get(event.toolCallId); if (tool) tools.set(event.toolCallId, { ...tool, result: event.content, status: toolResultFailed(event.content) ? "error" : "complete" }); }
  }
  return [...tools.values()];
}
