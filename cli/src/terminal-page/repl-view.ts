import type { TimelineItem, TimelineState } from "../application/event-projector.js";
import stringWidth from "string-width";

export function canFlushPromptQueue(runStatus: TimelineState["runStatus"]): boolean {
  return runStatus !== "running" && runStatus !== "waiting_input";
}

export function isSettledTimelineItem(item: TimelineItem): boolean {
  if (item.kind === "user") return !item.id.startsWith("queued-");
  if (item.kind === "error") return true;
  if (item.kind === "approval") {
    return !["pending", "submitting", "unknown_outcome", "resume_failed", "error"].includes(item.approval.status);
  }
  return item.status === "complete" || item.status === "error" || item.status === "stopped";
}

/**
 * 已完成的前缀写入终端原生滚动区；第一个未完成条目及其之后留在动态区。
 * 这样提示符始终在底部，上文用鼠标/触控板/终端滚动查看，而不是 PgUp 翻页。
 */
export function splitSettledTimeline(items: readonly TimelineItem[]): {
  settled: TimelineItem[];
  live: TimelineItem[];
} {
  let index = 0;
  while (index < items.length && isSettledTimelineItem(items[index]!)) {
    index += 1;
  }
  return { settled: items.slice(0, index), live: items.slice(index) };
}

export function runningActivityHint(items: readonly TimelineItem[]): string {
  const active = [...items].reverse().find((item) =>
    (item.kind === "tool" || item.kind === "thinking" || item.kind === "text")
    && item.status === "active",
  );
  if (active?.kind === "tool") return active.name;
  if (active?.kind === "thinking") return active.title || "思考";
  if (active?.kind === "text") return "回复中";
  return "正在运行";
}

export function visibleTimelineSlice<T>(
  items: readonly T[],
  scrollOffset: number,
  windowSize: number,
): { items: T[]; offset: number; hiddenAbove: number } {
  const size = Math.max(1, windowSize);
  const offset = Math.max(0, Math.min(scrollOffset, Math.max(0, items.length - 1)));
  const end = Math.max(0, items.length - offset);
  const start = Math.max(0, end - size);
  return {
    items: items.slice(start, end) as T[],
    offset,
    hiddenAbove: start,
  };
}

export function estimateTimelineItemLines(
  item: TimelineItem,
  columns: number,
  textMaxLines: number,
  expanded: boolean,
): number {
  const width = Math.max(24, columns - 6);
  if (item.kind === "user") {
    return Math.max(1, Math.ceil(Math.max(1, stringWidth(item.content)) / width));
  }
  if (item.kind === "text") {
    const content = item.content || "…";
    const hard = content.split("\n").reduce((sum, line) => sum + Math.max(1, Math.ceil(Math.max(1, stringWidth(line)) / width)), 0);
    return expanded ? Math.min(hard, 48) : Math.min(hard, Math.max(1, textMaxLines));
  }
  if (item.kind === "thinking" || item.kind === "tool" || item.kind === "approval") return 1;
  return Math.max(1, Math.ceil(Math.max(1, stringWidth(item.content)) / width));
}

/**
 * 按行高从底部取能放下的条目。按条数截会把长回复挤出屏幕，看起来像「前几轮没了」。
 */
export function visibleTimelineByBudget(
  items: readonly TimelineItem[],
  scrollOffset: number,
  lineBudget: number,
  estimate: (item: TimelineItem) => number,
): { items: TimelineItem[]; offset: number; hiddenAbove: number } {
  const offset = Math.max(0, Math.min(scrollOffset, Math.max(0, items.length - 1)));
  const end = Math.max(0, items.length - offset);
  const budget = Math.max(4, lineBudget);
  const picked: TimelineItem[] = [];
  let used = 0;
  for (let index = end - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (!item) continue;
    const lines = Math.max(1, estimate(item));
    if (picked.length > 0 && used + lines > budget) break;
    picked.unshift(item);
    used += lines;
  }
  return {
    items: picked,
    offset,
    hiddenAbove: end - picked.length,
  };
}

export function previewText(content: string, maxLines: number, columns: number): string {
  const width = Math.max(24, columns - 6);
  const source = content.split("\n");
  const lines: string[] = [];
  for (const line of source) {
    const chunks = Math.max(1, Math.ceil(Math.max(1, stringWidth(line)) / width));
    if (lines.length + chunks > maxLines) {
      lines.push(`${line.slice(0, Math.max(1, width - 1))}…`);
      break;
    }
    lines.push(line);
  }
  const joined = lines.join("\n");
  return joined === content ? content : joined.endsWith("…") ? joined : `${joined}\n…`;
}
