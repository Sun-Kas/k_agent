/** Parse Edit/Write/NotebookEdit tool arguments into a line-level diff for the chat card. */

export const MAX_DIFF_LINES = 400;
export const HUNK_CONTEXT = 3;

export type DiffLineKind = "add" | "del" | "ctx";

export type DiffLine = {
  kind: DiffLineKind;
  text: string;
  oldLine?: number;
  newLine?: number;
};

export type FileChangeHunk = {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  lines: DiffLine[];
};

export type FileChangePreviewModel = {
  toolName: string;
  path: string;
  subtitle?: string;
  hunks: FileChangeHunk[];
  added: number;
  removed: number;
  truncated: boolean;
};

const FILE_WRITE_TOOLS = new Set(["Edit", "Write", "NotebookEdit"]);

export function isFileWriteTool(name: string): boolean {
  return FILE_WRITE_TOOLS.has(name);
}

export function parseFileChange(
  toolName: string,
  argumentsJson: string,
  resultJson?: string,
): FileChangePreviewModel | null {
  if (!FILE_WRITE_TOOLS.has(toolName)) return null;
  const payload = parseObject(argumentsJson);
  const result = parseObject(resultJson ?? "");
  const path = pickString(payload ?? {}, ["file_path", "filePath", "path", "notebook_path"])
    || pickString(result ?? {}, ["path", "file_path"]);
  const fromResult = result ? parseResultHunks(result) : null;
  if (fromResult && fromResult.length > 0) {
    const lines = fromResult.flatMap((hunk) => hunk.lines);
    return {
      toolName,
      path: path || "(unknown path)",
      hunks: fromResult,
      added: lines.filter((line) => line.kind === "add").length,
      removed: lines.filter((line) => line.kind === "del").length,
      truncated: fromResult.some((hunk) => hunk.lines.length >= MAX_DIFF_LINES),
    };
  }
  if (!payload) return null;
  if (toolName === "Edit") {
    if (!hasKey(payload, "old_string") && !hasKey(payload, "oldString")) return null;
    return buildModel(
      toolName,
      path,
      pickString(payload, ["old_string", "oldString"]),
      pickString(payload, ["new_string", "newString"]),
    );
  }
  if (toolName === "Write") {
    if (!hasKey(payload, "content")) return null;
    return buildModel(toolName, path, "", pickString(payload, ["content"]));
  }
  const mode = pickString(payload, ["mode"]) || "replace";
  const source = pickString(payload, ["source"]);
  const cell = payload.cell_index ?? payload.cellIndex;
  const subtitle = typeof cell === "number" || typeof cell === "string"
    ? `cell ${cell} · ${mode}`
    : mode;
  if (mode === "delete") {
    return buildModel(toolName, path, source || "(deleted cell)", "", subtitle);
  }
  return buildModel(toolName, path, "", source, subtitle);
}

export function lineDiff(oldText: string, newText: string): DiffLine[] {
  const previous = splitLines(oldText);
  const next = splitLines(newText);
  if (previous.length === 0 && next.length === 0) return [];
  if (previous.length === 0) return next.map((text) => ({ kind: "add" as const, text }));
  if (next.length === 0) return previous.map((text) => ({ kind: "del" as const, text }));
  if (previous.length * next.length > 80_000) {
    return [
      ...previous.map((text) => ({ kind: "del" as const, text })),
      ...next.map((text) => ({ kind: "add" as const, text })),
    ];
  }
  return lcsDiff(previous, next);
}

function parseResultHunks(result: Record<string, unknown>): FileChangeHunk[] | null {
  if (!Array.isArray(result.hunks)) return null;
  const hunks: FileChangeHunk[] = [];
  for (const raw of result.hunks) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const record = raw as Record<string, unknown>;
    if (!Array.isArray(record.lines)) continue;
    const lines: DiffLine[] = [];
    for (const item of record.lines) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const line = item as Record<string, unknown>;
      const kind = line.kind;
      if (kind !== "add" && kind !== "del" && kind !== "ctx") continue;
      if (typeof line.text !== "string") continue;
      lines.push({
        kind,
        text: line.text,
        oldLine: asPositiveInt(line.oldLine),
        newLine: asPositiveInt(line.newLine),
      });
    }
    if (!lines.length) continue;
    hunks.push({
      oldStart: asPositiveInt(record.oldStart) ?? lines.find((line) => line.oldLine)?.oldLine ?? 1,
      oldCount: asPositiveInt(record.oldCount) ?? lines.filter((line) => line.kind !== "add").length,
      newStart: asPositiveInt(record.newStart) ?? lines.find((line) => line.newLine)?.newLine ?? 1,
      newCount: asPositiveInt(record.newCount) ?? lines.filter((line) => line.kind !== "del").length,
      lines,
    });
  }
  return hunks.length ? hunks : null;
}

function buildModel(
  toolName: string,
  path: string,
  oldText: string,
  newText: string,
  subtitle?: string,
): FileChangePreviewModel {
  const collapsed = collapseAroundChanges(lineDiff(oldText, newText), HUNK_CONTEXT);
  const added = collapsed.filter((line) => line.kind === "add").length;
  const removed = collapsed.filter((line) => line.kind === "del").length;
  const truncated = collapsed.length > MAX_DIFF_LINES;
  const lines = truncated ? collapsed.slice(0, MAX_DIFF_LINES) : collapsed;
  return {
    toolName,
    path: path || "(unknown path)",
    subtitle,
    hunks: [{
      oldStart: 0,
      oldCount: lines.filter((line) => line.kind !== "add").length,
      newStart: 0,
      newCount: lines.filter((line) => line.kind !== "del").length,
      lines,
    }],
    added,
    removed,
    truncated,
  };
}

function collapseAroundChanges(lines: DiffLine[], context: number): DiffLine[] {
  const changeIndexes = lines.flatMap((line, index) => line.kind === "ctx" ? [] : [index]);
  if (changeIndexes.length === 0) return [];
  const keep = new Set<number>();
  for (const index of changeIndexes) {
    const from = Math.max(0, index - context);
    const to = Math.min(lines.length - 1, index + context);
    for (let cursor = from; cursor <= to; cursor += 1) keep.add(cursor);
  }
  return [...keep].sort((left, right) => left - right).map((index) => lines[index]);
}

function lcsDiff(previous: string[], next: string[]): DiffLine[] {
  const rows = previous.length;
  const cols = next.length;
  const table: Uint16Array[] = Array.from({ length: rows + 1 }, () => new Uint16Array(cols + 1));
  for (let i = 1; i <= rows; i += 1) {
    for (let j = 1; j <= cols; j += 1) {
      table[i][j] = previous[i - 1] === next[j - 1]
        ? table[i - 1][j - 1] + 1
        : Math.max(table[i - 1][j], table[i][j - 1]);
    }
  }
  const reversed: DiffLine[] = [];
  let i = rows;
  let j = cols;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && previous[i - 1] === next[j - 1]) {
      reversed.push({ kind: "ctx", text: previous[i - 1] });
      i -= 1;
      j -= 1;
    } else if (j > 0 && (i === 0 || table[i][j - 1] >= table[i - 1][j])) {
      reversed.push({ kind: "add", text: next[j - 1] });
      j -= 1;
    } else {
      reversed.push({ kind: "del", text: previous[i - 1] });
      i -= 1;
    }
  }
  reversed.reverse();
  return reversed;
}

function splitLines(text: string): string[] {
  if (text.length === 0) return [];
  return text.split("\n");
}

function parseObject(raw: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(raw) as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return value as Record<string, unknown>;
  } catch {
    return null;
  }
}

function hasKey(payload: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(payload, key);
}

function pickString(payload: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string") return value;
  }
  return "";
}

function asPositiveInt(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isInteger(value) && value > 0) return value;
  return undefined;
}
