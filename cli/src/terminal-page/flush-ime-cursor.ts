const BEGIN_SYNC = "\u001b[?2026h";
const END_SYNC = "\u001b[?2026l";

/**
 * Ink 把「把真实光标移回输入框」这条指令包在同步更新（DEC 2026）里单独发送。
 * 一部分终端会丢掉同步块中的光标移动，光标停在帧尾下一行的第 0 列，
 * 输入法就把拼音画在那里，和输入框里已经上屏的汉字对不齐。
 * 同步括号在 Ink 里是一次独立 write，这里只丢掉这两次括号，光标指令仍只写一次。
 */
export function revealImeCursor(stdout: NodeJS.WriteStream): void {
  const original = stdout.write.bind(stdout) as (...args: unknown[]) => boolean;
  const finish = (encoding?: unknown, callback?: unknown) => {
    const done = typeof callback === "function" ? callback : typeof encoding === "function" ? encoding : undefined;
    if (done) (done as () => void)();
  };
  stdout.write = ((chunk: unknown, encoding?: unknown, callback?: unknown) => {
    if (chunk === BEGIN_SYNC || chunk === END_SYNC) {
      finish(encoding, callback);
      return true;
    }
    if (typeof callback === "function") return original(chunk, encoding, callback);
    if (encoding !== undefined) return original(chunk, encoding);
    return original(chunk);
  }) as NodeJS.WriteStream["write"];
}
