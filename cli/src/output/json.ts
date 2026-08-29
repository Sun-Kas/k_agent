import { sanitizeTerminalContent } from "./sanitize.js";

/** JSON/JSONL 也清理控制字符，防止日志被 OSC 标题或光标序列污染。 */
export function safeJson(value: unknown): string {
  return JSON.stringify(sanitizeValue(value));
}

function sanitizeValue(value: unknown): unknown {
  if (typeof value === "string") return sanitizeTerminalContent(value);
  if (Array.isArray(value)) return value.map(sanitizeValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sanitizeValue(item)]));
  }
  return value;
}
