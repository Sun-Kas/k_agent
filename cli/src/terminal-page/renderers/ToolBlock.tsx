import React from "react";
import { Box, Text, useStdout } from "ink";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import type { TimelineItem } from "../../application/event-projector.js";
import { TERMINAL_DESIGN, terminalLayout } from "../design.js";
import {
  CLI_DIFF_EXPANDED_LINES,
  CLI_DIFF_PREVIEW_LINES,
  displayLineNumber,
  parseFileChange,
  shortPath,
  takeDiffLines,
  type DiffLine,
  type FileChangePreviewModel,
} from "../../tools/file-change-preview.js";

export function ToolBlock({ item, expanded = false }: { item: Extract<TimelineItem, { kind: "tool" }>; expanded?: boolean }): React.ReactElement {
  const change = parseFileChange(item.name, item.arguments, item.result);
  const color = item.status === "error"
    ? TERMINAL_DESIGN.colors.danger
    : item.status === "complete"
      ? TERMINAL_DESIGN.colors.success
      : TERMINAL_DESIGN.colors.accent;
  if (change) {
    return <FileChangeToolBlock change={change} color={color} expanded={expanded} status={item.status} />;
  }
  return (
    <Box flexDirection="column">
      <Text color={color}>{TERMINAL_DESIGN.symbols.tool} {sanitizeTerminalContent(item.name)} · {item.status}</Text>
      {expanded && item.arguments ? <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">参数  {sanitizeTerminalContent(item.arguments)}</Text> : null}
      {expanded && item.liveOutput ? <Text wrap="truncate-end">输出  {sanitizeTerminalContent(item.liveOutput)}</Text> : null}
      {expanded && item.result ? <Text wrap="truncate-end">结果  {sanitizeTerminalContent(item.result)}</Text> : null}
    </Box>
  );
}

function FileChangeToolBlock({
  change,
  color,
  expanded,
  status,
}: {
  change: FileChangePreviewModel;
  color: typeof TERMINAL_DESIGN.colors[keyof typeof TERMINAL_DESIGN.colors];
  expanded: boolean;
  status: string;
}): React.ReactElement {
  const { stdout } = useStdout();
  const columns = stdout.columns ?? 80;
  // 父级 Static 有 paddingX=1；框必须按终端列数封顶，否则长 diff 会把圆角框撑破换行。
  const boxWidth = Math.max(24, columns - 4);
  const compact = terminalLayout(columns) === "compact" || terminalLayout(columns) === "minimum";
  const maxLines = expanded ? CLI_DIFF_EXPANDED_LINES : CLI_DIFF_PREVIEW_LINES;
  const { lines, hidden } = takeDiffLines(change, maxLines);
  const path = compact
    ? (shortPath(change.path).split("/").at(-1) ?? shortPath(change.path))
    : shortPath(change.path);
  const borderColor = status === "error" ? TERMINAL_DESIGN.colors.danger : TERMINAL_DESIGN.colors.muted;
  return (
    <Box
      flexDirection="column"
      width={boxWidth}
      overflow="hidden"
      borderStyle={TERMINAL_DESIGN.borders.panel}
      borderColor={borderColor}
      borderLeft
      borderRight={!compact}
      borderTop={!compact}
      borderBottom={!compact}
      paddingX={1}
    >
      <Text color={color} wrap="truncate-end">
        {TERMINAL_DESIGN.symbols.tool} {sanitizeTerminalContent(change.toolName)}  {sanitizeTerminalContent(path)}  +{change.added} -{change.removed} · {status}
      </Text>
      {lines.map((line, index) => (
        <DiffLineRow line={line} key={`${index}-${line.kind}-${line.oldLine ?? 0}-${line.newLine ?? 0}`} />
      ))}
      {hidden > 0 || change.truncated ? (
        <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">… {hidden || "more"} lines</Text>
      ) : null}
    </Box>
  );
}

function DiffLineRow({ line }: { line: DiffLine }): React.ReactElement {
  const number = String(displayLineNumber(line) ?? "").padStart(4);
  const mark = line.kind === "add" ? "+" : line.kind === "del" ? "-" : " ";
  const color = line.kind === "add"
    ? TERMINAL_DESIGN.colors.success
    : line.kind === "del"
      ? TERMINAL_DESIGN.colors.danger
      : TERMINAL_DESIGN.colors.muted;
  return (
    <Text color={color} wrap="truncate-end">
      {number} {mark} {sanitizeTerminalContent(line.text)}
    </Text>
  );
}
