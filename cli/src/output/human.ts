import type { AgUiEvent } from "../protocol/index.js";
import { sanitizeTerminalContent } from "./sanitize.js";
import { CLI_DIFF_PREVIEW_LINES, formatFileChangePlain, parseFileChange } from "../tools/file-change-preview.js";

const pendingTools = new Map<string, { name: string; arguments: string }>();

/** stdout 只承载助手正文；运行状态和诊断固定写 stderr，供 shell 管道稳定消费。 */
export function writeHumanEvent(event: AgUiEvent, quiet: boolean): void {
  if (event.type === "TEXT_MESSAGE_CONTENT") {
    process.stdout.write(sanitizeTerminalContent(event.delta));
    return;
  }
  if (quiet) return;
  if (event.type === "REASONING_START") process.stderr.write("[thinking] 开始\n");
  if (event.type === "TOOL_CALL_START") {
    pendingTools.set(event.toolCallId, { name: event.toolCallName, arguments: "" });
    process.stderr.write(`[tool] ${sanitizeTerminalContent(event.toolCallName)}\n`);
    return;
  }
  if (event.type === "TOOL_CALL_ARGS") {
    const pending = pendingTools.get(event.toolCallId);
    if (pending) pending.arguments += event.delta;
    return;
  }
  if (event.type === "TOOL_CALL_RESULT") {
    const pending = pendingTools.get(event.toolCallId);
    pendingTools.delete(event.toolCallId);
    const change = parseFileChange(pending?.name ?? "", pending?.arguments ?? "", event.content);
    if (change) {
      process.stderr.write(formatFileChangePlain(change, CLI_DIFF_PREVIEW_LINES, process.stderr.columns ?? 80));
      return;
    }
    process.stderr.write("[tool] 完成\n");
    return;
  }
  if (event.type === "RUN_ERROR") process.stderr.write(`[error] ${sanitizeTerminalContent(event.message)}\n`);
}
