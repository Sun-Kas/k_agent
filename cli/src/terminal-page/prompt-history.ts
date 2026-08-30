/**
 * 本进程内的提示符历史。不写磁盘，只让 ↑↓ 符合终端习惯。
 */

export function pushPromptHistory(history: readonly string[], text: string): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [...history];
  if (history[history.length - 1] === trimmed) return [...history];
  return [...history, trimmed];
}

/** cursor = -1 表示正在编辑的草稿；0 是最近一条已发送内容。 */
export function promptHistoryText(
  history: readonly string[],
  cursor: number,
  stash: string,
): string | undefined {
  if (cursor < -1 || cursor >= history.length) return undefined;
  if (cursor === -1) return stash;
  return history[history.length - 1 - cursor];
}
