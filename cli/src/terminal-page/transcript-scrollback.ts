import type { TimelineItem } from "../application/event-projector.js";

const CLEAR_TERMINAL = "\u001b[2J\u001b[3J\u001b[H";

/** 对话页用会话 id 区分滚动区；还没创建会话时用 draft。其它页面都不保留这段记录。 */
export function transcriptOwner(surface: string, sessionId: string | undefined): string {
  if (surface !== "chat") return "away";
  return sessionId ?? "draft";
}

/**
 * 离开当前对话、或从一个已有会话换到另一个会话时，清掉已经打印的滚动区。
 * 草稿第一次拿到真实 id 仍是同一段对话，不能把刚写上的内容清掉。
 */
export function shouldResetTranscriptScrollback(previous: string | undefined, next: string): boolean {
  if (previous === undefined || previous === next) return false;
  if (previous === "draft" && next !== "away" && next !== "draft") return false;
  return true;
}

export function staticTranscriptKey(owner: string, item: TimelineItem): string {
  return `${owner}:${item.id}:${item.sequence}`;
}

/**
 * Ink 的擦除只覆盖动态帧，Static 打印过的行会留在上面。
 * 离开会话时把这次带擦除序列的写入改成整屏清除，再画新页面。
 * 返回 undefined 表示这次写入不是帧更新，先不动。
 */
export function rewriteFrameAfterTranscriptWipe(chunk: string): { chunk: string; consumed: boolean } | undefined {
  if (chunk.startsWith("\u001b[2J")) return { chunk, consumed: true };
  const eraseLine = "\u001b[2K";
  const at = chunk.lastIndexOf(eraseLine);
  if (at < 0) return undefined;
  const cursorLeft = chunk.indexOf("\u001b[G", at);
  if (cursorLeft < 0) return undefined;
  return { chunk: CLEAR_TERMINAL + chunk.slice(cursorLeft + "\u001b[G".length), consumed: true };
}
