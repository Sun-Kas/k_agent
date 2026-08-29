import type { AgUiEvent } from "../protocol/index.js";
import { sanitizeTerminalContent } from "./sanitize.js";

/** stdout 只承载助手正文；运行状态和诊断固定写 stderr，供 shell 管道稳定消费。 */
export function writeHumanEvent(event: AgUiEvent, quiet: boolean): void {
  if (event.type === "TEXT_MESSAGE_CONTENT") {
    process.stdout.write(sanitizeTerminalContent(event.delta));
    return;
  }
  if (quiet) return;
  if (event.type === "REASONING_START") process.stderr.write("[thinking] 开始\n");
  if (event.type === "TOOL_CALL_START") process.stderr.write(`[tool] ${sanitizeTerminalContent(event.toolCallName)}\n`);
  if (event.type === "TOOL_CALL_RESULT") process.stderr.write("[tool] 完成\n");
  if (event.type === "RUN_ERROR") process.stderr.write(`[error] ${sanitizeTerminalContent(event.message)}\n`);
}
