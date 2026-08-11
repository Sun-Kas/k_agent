import { useEffect, useMemo, useState } from "react";

import { resolveApproval } from "../api/agui";
import type { AgUiEvent, ApprovalActivity, ChatMessage, SessionState, ToolActivity } from "../types";
import { MarkdownContent } from "./MarkdownContent";
import { timelineFromEvents, type StaticTimelineActivity } from "./transcript-timeline";

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
  const timeline = useMemo(() => timelineFromEvents(session.events ?? []), [session.events]);
  const userMessages = messages.filter((message) => message.role === "user");
  const assistantMessages = messages.filter((message) => message.role === "assistant");
  const fallbackAnswer = assistantMessages.map((message) => message.content).filter(Boolean).join("\n\n");
  const approvals = useMemo(() => approvalsFromEvents(session.events ?? []), [session.events]);
  return <div className="scheduled-transcript">{userMessages.map((message) =>
    <article className={`message-row ${message.role}`} key={message.id}>
      <div className="avatar">你</div>
      <div className="message-body">
        <div className="bubble"><MarkdownContent content={message.content} /></div>
      </div>
    </article>)}
    {(timeline.length > 0 || fallbackAnswer || approvals.length > 0) && <article className="message-row assistant">
      <div className="avatar">K</div>
      <div className="message-body">
        {timeline.length > 0
          ? timeline.map((activity) => activity.type === "thinking"
            ? <StaticThinkingActivity activity={activity} key={activity.id} />
            : activity.type === "tool"
              ? <InlineToolActivity tool={activity.tool} key={activity.id} />
              : <div className="assistant-output" key={activity.id}><MarkdownContent content={activity.content} /></div>)
          : <div className="assistant-output"><MarkdownContent content={fallbackAnswer} /></div>}
        {approvals.map((approval) => <StaticApprovalCard approval={approval} key={approval.id} />)}
      </div>
    </article>}
  </div>;
}

function approvalsFromEvents(events: AgUiEvent[]): ApprovalActivity[] {
  const approvals = new Map<string, ApprovalActivity>();
  let sequence = 0;
  for (const event of events) {
    if (event.type !== "CUSTOM") continue;
    if (event.name === "approval_request") {
      const id = String(event.value.id ?? "");
      const runId = String(event.value.runId ?? "");
      if (!id || !runId || approvals.has(id)) continue;
      approvals.set(id, {
        id,
        threadId: String(event.value.threadId ?? ""),
        runId,
        agentKind: String(event.value.agentKind ?? "k_agent"),
        category: String(event.value.category ?? "tool"),
        title: String(event.value.title ?? "需要你的确认"),
        message: String(event.value.message ?? "请确认是否继续。"),
        detail: event.value.detail && typeof event.value.detail === "object"
          ? event.value.detail as Record<string, unknown>
          : {},
        status: "pending",
        sequence: sequence += 1
      });
    } else if (event.name === "approval_resolved") {
      const id = String(event.value.id ?? "");
      const existing = approvals.get(id);
      if (!existing) continue;
      const action = String(event.value.action ?? "cancel");
      approvals.set(id, { ...existing, status: action === "approve" ? "approved" : action === "deny" ? "denied" : "cancelled" });
    }
  }
  return [...approvals.values()];
}

/** 定时任务在原页面内完成 HITL；审批后轮询到的持久化事件会成为最终状态。 */
function StaticApprovalCard({ approval }: { approval: ApprovalActivity }) {
  const [status, setStatus] = useState(approval.status);
  const [error, setError] = useState("");
  useEffect(() => { setStatus(approval.status); }, [approval.status]);
  const pending = status === "pending" || status === "error";
  const preview = approval.detail.command ?? approval.detail.arguments;

  async function decide(action: "approve" | "deny", remember = false) {
    setStatus("submitting");
    setError("");
    try {
      await resolveApproval(approval.id, {
        threadId: approval.threadId,
        runId: approval.runId,
        action,
        remember
      });
      setStatus(action === "approve" ? "approved" : "denied");
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "审批提交失败");
    }
  }

  return <section className={`approval-card ${status}`} aria-live="polite">
    <header><span className="approval-shield" aria-hidden="true">!</span><div><small>{approval.agentKind} · {approval.category}</small><strong>{approval.title}</strong></div><b>{status === "pending" ? "等待确认" : status === "submitting" ? "正在提交" : status === "approved" ? "已允许" : status === "denied" ? "已拒绝" : "提交失败"}</b></header>
    <p>{approval.message}</p>
    {preview !== undefined && <code>{typeof preview === "string" ? preview : JSON.stringify(preview, null, 2)}</code>}
    {error && <p className="approval-error">{error}</p>}
    {pending && <footer><button type="button" onClick={() => void decide("deny")}>拒绝</button><button type="button" onClick={() => void decide("approve")}>允许一次</button><button className="primary" type="button" onClick={() => void decide("approve", true)}>本轮始终允许</button></footer>}
  </section>;
}

function StaticThinkingActivity({ activity }: { activity: Extract<StaticTimelineActivity, { type: "thinking" }> }) {
  const [open, setOpen] = useState(false);
  return <section className={`inline-thinking ${activity.status} ${open ? "open" : ""}`}>
    <button type="button" className="inline-thinking-summary" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span>{activity.status === "complete" ? "已完成思考" : "思考过程"}</span>
      {activity.status === "running" && <b>进行中</b>}
      <i aria-hidden="true">⌃</i>
    </button>
    {open && <div className="inline-thinking-list"><article className={`inline-thinking-step ${activity.status}`}><span aria-hidden="true">{activity.status === "complete" ? "✓" : "✦"}</span><div><strong>{activity.title || "思考过程"}</strong>{activity.detail && <p>{activity.detail}</p>}</div></article></div>}
  </section>;
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
