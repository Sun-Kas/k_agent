import stripAnsi from "strip-ansi";

/**
 * 模型和工具输出属于不可信文本。除 ANSI 外继续移除 OSC 与不可见控制字符，
 * 防止内容移动光标、改写窗口标题或伪造终端页面区域。
 */
export function sanitizeTerminalContent(value: string): string {
  return stripAnsi(value)
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, "")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
}
