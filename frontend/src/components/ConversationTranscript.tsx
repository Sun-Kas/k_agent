import { useEffect, useMemo, useRef, useState } from "react";

import { getApprovalStatus, resolveApproval } from "../api/agui";
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

function InlineToolIcon() {
  // A vector icon keeps the tool marker centered across platform fonts; the former
  // text glyph had an asymmetric font box and visibly drifted below the label.
  return <svg className="inline-tool-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M11.7 4.1a4.1 4.1 0 0 0-4.9 5.3l-4.4 4.4a1.7 1.7 0 1 0 2.4 2.4l4.4-4.4a4.1 4.1 0 0 0 5.3-4.9l-2.5 2.5-1.9-1.9 2.5-2.5a4 4 0 0 0-.9-.9Z" />
  </svg>;
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
    let value: Record<string, unknown>;
    let activityMessageId = "";
    if (event.type === "ACTIVITY_SNAPSHOT" && event.activityType === "approval") {
      value = event.content && typeof event.content === "object"
        ? event.content as Record<string, unknown>
        : {};
      activityMessageId = event.messageId;
    } else if (
      event.type === "CUSTOM"
      && (event.name === "approval_request" || event.name === "approval_resolved")
    ) {
      value = event.value;
    } else {
      continue;
    }
    const id = String(value.id ?? activityMessageId);
    const runId = String(value.runId ?? "");
    if (!id || !runId) continue;
    const existing = approvals.get(id);
    if (!existing) {
      approvals.set(id, {
        id,
        threadId: String(value.threadId ?? ""),
        runId,
        agentKind: String(value.agentKind ?? "k_agent"),
        category: String(value.category ?? "tool"),
        title: String(value.title ?? "需要你的确认"),
        message: String(value.message ?? "请确认是否继续。"),
        detail: value.detail && typeof value.detail === "object"
          ? value.detail as Record<string, unknown>
          : {},
        status: "pending",
        sequence: sequence += 1
      });
    }
    const current = approvals.get(id);
    if (!current) continue;
    const action = String(value.action ?? "");
    const status = String(value.status ?? "");
    approvals.set(id, {
      ...current,
      status: status === "approved" || action === "approve"
        ? "approved"
        : status === "denied" || action === "deny"
          ? "denied"
          : status === "expired" || action === "expired"
            ? "expired"
            : status === "cancelled" || action === "cancel"
              ? "cancelled"
              : "pending"
    });
  }
  return [...approvals.values()];
}

/** 定时任务在原页面内完成 HITL；审批后轮询到的持久化事件会成为最终状态。 */
function StaticApprovalCard({ approval }: { approval: ApprovalActivity }) {
  const [status, setStatus] = useState(approval.status);
  const [error, setError] = useState("");
  useEffect(() => { setStatus(approval.status); }, [approval.status]);
  useEffect(() => {
    if (approval.status !== "pending") return;
    let active = true;
    void getApprovalStatus(approval.id, {
      threadId: approval.threadId,
      runId: approval.runId
    }).then(({ pending: stillPending }) => {
      if (active && !stillPending) {
        setStatus("expired");
        setError("该审批已失效，请重新发起任务。");
      }
    }).catch(() => undefined);
    return () => { active = false; };
  }, [approval.id, approval.runId, approval.status, approval.threadId]);
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
      const message = reason instanceof Error ? reason.message : "审批提交失败";
      const expired = /no longer pending|不再等待|已失效/i.test(message);
      setStatus(expired ? "expired" : "error");
      setError(expired ? "该审批已失效，请重新发起任务。" : message);
    }
  }

  return <section className={`approval-card ${status}`} aria-live="polite">
    <header><span className="approval-shield" aria-hidden="true">!</span><div><small>{approval.agentKind} · {approval.category}</small><strong>{approval.title}</strong></div><b>{status === "pending" ? "等待确认" : status === "submitting" ? "正在提交" : status === "approved" ? "已允许" : status === "denied" ? "已拒绝" : status === "cancelled" ? "已取消" : status === "expired" ? "已失效" : "提交失败"}</b></header>
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
      <span>{activity.status === "complete" ? "已完成思考" : activity.status === "stopped" ? "思考已停止" : "思考过程"}</span>
      {activity.status === "running" && <b>进行中</b>}
      <i aria-hidden="true">⌃</i>
    </button>
    {open && <div className="inline-thinking-list"><article className={`inline-thinking-step ${activity.status}`}><span aria-hidden="true">{activity.status === "complete" ? "✓" : "✦"}</span><div><strong>{activity.title || "思考过程"}</strong>{activity.detail && <p>{activity.detail}</p>}</div></article></div>}
  </section>;
}

export function InlineToolActivity({ tool }: { tool: ToolActivity }) {
  const [open, setOpen] = useState(tool.status !== "complete");
  const attemptedUrlRef = useRef("");
  const interactive = useMemo(() => {
    if (tool.executionMode === "interactive") return true;
    try { return JSON.parse(tool.arguments || "{}").execution_mode === "interactive"; }
    catch { return false; }
  }, [tool.arguments, tool.executionMode]);
  const externalUrl = useMemo(() => {
    if (!interactive) return "";
    const match = (tool.liveOutput ?? "").match(/https:\/\/[^\s<>"']+/i);
    return match?.[0] ?? "";
  }, [interactive, tool.liveOutput]);
  useEffect(() => { if (tool.status === "complete") setOpen(false); }, [tool.status]);
  useEffect(() => {
    if (!externalUrl || attemptedUrlRef.current === externalUrl) return;
    attemptedUrlRef.current = externalUrl;
    // Async stream events may be blocked by popup policies. The persistent
    // button below is therefore the source of truth; this is only a best effort.
    window.open(externalUrl, "_blank", "noopener,noreferrer");
  }, [externalUrl]);
  return <section className={`inline-tool ${tool.status} ${open ? "open" : ""}`}>
    <button type="button" className="inline-tool-summary" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <InlineToolIcon /><strong>调用工具</strong><code>{tool.name}</code>
      <b>{tool.status === "complete" ? "已完成" : tool.status === "error" ? "失败" : tool.status === "stopped" ? "已停止" : "运行中"}</b><i aria-hidden="true">⌃</i>
    </button>
    {open && (tool.arguments || tool.liveOutput || tool.result) && <div className="inline-tool-detail">
      {externalUrl && <div className="external-action-card" aria-live="polite">
        <div><strong>需要完成外部授权</strong><span>{tool.status === "running" ? "正在等待你在浏览器中完成授权，成功后任务会自动继续。" : "授权流程已结束。"}</span></div>
        <a href={externalUrl} target="_blank" rel="noreferrer">打开授权页面</a>
      </div>}
      {tool.arguments && <code>{tool.arguments}</code>}
      {tool.liveOutput && <pre className="inline-tool-live-output">{tool.liveOutput}</pre>}
      {tool.result && <p>{tool.result}</p>}
    </div>}
  </section>;
}
