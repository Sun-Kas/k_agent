/**
 * 终端单行/多行输入的纯编辑逻辑。
 *
 * 光标位置以 grapheme 为单位，不能用 JavaScript 字符下标：中文、Emoji 和组合
 * 字符会被 UTF-16 下标切断，导致删除半个字符或光标落在不可见位置。
 * 这里只做文本变换，不触发任何网络或 run 生命周期动作。
 */

const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export interface LineEditorState {
  readonly value: string;
  readonly cursor: number;
}

export function graphemesOf(value: string): string[] {
  return Array.from(graphemeSegmenter.segment(value), (part) => part.segment);
}

export function graphemeLength(value: string): number {
  return graphemesOf(value).length;
}

export function createLineEditor(value: string, cursor?: number): LineEditorState {
  const length = graphemeLength(value);
  return { value, cursor: clamp(cursor ?? length, length) };
}

export function textBeforeCursor(state: LineEditorState): string {
  const parts = graphemesOf(state.value);
  return parts.slice(0, clamp(state.cursor, parts.length)).join("");
}

export function graphemeAtCursor(state: LineEditorState): string {
  const parts = graphemesOf(state.value);
  return parts[clamp(state.cursor, parts.length)] ?? "";
}

export function textAfterCursor(state: LineEditorState): string {
  const parts = graphemesOf(state.value);
  return parts.slice(clamp(state.cursor, parts.length) + 1).join("");
}

export function insertText(state: LineEditorState, text: string): LineEditorState {
  if (!text) return state;
  const parts = graphemesOf(state.value);
  const cursor = clamp(state.cursor, parts.length);
  const inserted = graphemesOf(text);
  const next = [...parts.slice(0, cursor), ...inserted, ...parts.slice(cursor)];
  return { value: next.join(""), cursor: cursor + inserted.length };
}

export function deleteBackward(state: LineEditorState): LineEditorState {
  const parts = graphemesOf(state.value);
  const cursor = clamp(state.cursor, parts.length);
  if (cursor === 0) return state;
  const next = [...parts.slice(0, cursor - 1), ...parts.slice(cursor)];
  return { value: next.join(""), cursor: cursor - 1 };
}

export function deleteForward(state: LineEditorState): LineEditorState {
  const parts = graphemesOf(state.value);
  const cursor = clamp(state.cursor, parts.length);
  if (cursor >= parts.length) return state;
  const next = [...parts.slice(0, cursor), ...parts.slice(cursor + 1)];
  return { value: next.join(""), cursor };
}

/** Ctrl+W：先吃掉光标左侧空白，再吃掉一个完整词，行为对齐常见 shell。 */
export function deleteWordBackward(state: LineEditorState): LineEditorState {
  const parts = graphemesOf(state.value);
  let cursor = clamp(state.cursor, parts.length);
  while (cursor > 0 && isBlank(parts[cursor - 1])) cursor -= 1;
  while (cursor > 0 && !isBlank(parts[cursor - 1])) cursor -= 1;
  const next = [...parts.slice(0, cursor), ...parts.slice(clamp(state.cursor, parts.length))];
  return { value: next.join(""), cursor };
}

export function deleteToLineStart(state: LineEditorState): LineEditorState {
  const parts = graphemesOf(state.value);
  const cursor = clamp(state.cursor, parts.length);
  return { value: parts.slice(cursor).join(""), cursor: 0 };
}

export function deleteToLineEnd(state: LineEditorState): LineEditorState {
  const parts = graphemesOf(state.value);
  const cursor = clamp(state.cursor, parts.length);
  return { value: parts.slice(0, cursor).join(""), cursor };
}

export function moveLeft(state: LineEditorState): LineEditorState {
  return { value: state.value, cursor: Math.max(0, clamp(state.cursor, graphemeLength(state.value)) - 1) };
}

export function moveRight(state: LineEditorState): LineEditorState {
  const length = graphemeLength(state.value);
  return { value: state.value, cursor: Math.min(length, clamp(state.cursor, length) + 1) };
}

export function moveWordLeft(state: LineEditorState): LineEditorState {
  const parts = graphemesOf(state.value);
  let cursor = clamp(state.cursor, parts.length);
  while (cursor > 0 && isBlank(parts[cursor - 1])) cursor -= 1;
  while (cursor > 0 && !isBlank(parts[cursor - 1])) cursor -= 1;
  return { value: state.value, cursor };
}

export function moveWordRight(state: LineEditorState): LineEditorState {
  const parts = graphemesOf(state.value);
  let cursor = clamp(state.cursor, parts.length);
  while (cursor < parts.length && isBlank(parts[cursor])) cursor += 1;
  while (cursor < parts.length && !isBlank(parts[cursor])) cursor += 1;
  return { value: state.value, cursor };
}

export function moveToStart(state: LineEditorState): LineEditorState {
  return { value: state.value, cursor: 0 };
}

export function moveToEnd(state: LineEditorState): LineEditorState {
  return { value: state.value, cursor: graphemeLength(state.value) };
}

function isBlank(segment: string | undefined): boolean {
  return segment === undefined || /\s/.test(segment);
}

function clamp(cursor: number, length: number): number {
  if (!Number.isFinite(cursor) || cursor < 0) return 0;
  return Math.min(Math.floor(cursor), length);
}
