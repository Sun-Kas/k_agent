/**
 * Ink 交互帧会把真实光标停在输入行（IME），unmount 时 `log.done()` 只显示光标、不回到帧底。
 * 随后父进程往 stderr 写一行就会叠在残留 TUI 上。退出后先把光标送到视口底部再换行。
 */
export const RESTORE_TERMINAL_SEQUENCE =
  "\u001b[?25h" + // show cursor
  "\u001b[9999B" + // cursor down; stops at the bottom margin
  "\u001b[1G" + // column 1
  "\n";

export function restoreTerminalAfterTui(
  stdout: NodeJS.WriteStream = process.stdout,
  stdin: NodeJS.ReadStream = process.stdin,
): void {
  if (stdout.isTTY) stdout.write(RESTORE_TERMINAL_SEQUENCE);
  try {
    if (stdin.isTTY && stdin.isRaw) stdin.setRawMode(false);
  } catch {
    // 进程已被信号打断时，stdin 可能已经不是 raw。
  }
}
