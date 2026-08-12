import type { ChatMessage } from "./types";

/**
 * 审批可能紧跟 RUN_STARTED 到达，而 React 尚未提交占位 assistant 的 runId。
 * 先把当前 run 绑定到这条占位消息，审批卡才有稳定的时间线宿主。
 */
export function bindApprovalRunToAssistant(
  messages: ChatMessage[],
  runId: string,
  pendingAssistantId: string | null
): ChatMessage[] {
  if (messages.some((message) => message.role === "assistant" && message.meta?.runId === runId)) {
    return messages;
  }

  const pendingIndex = pendingAssistantId
    ? messages.findIndex((message) => message.id === pendingAssistantId && message.role === "assistant")
    : -1;
  let fallbackIndex = pendingIndex;
  if (fallbackIndex < 0) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.role === "assistant" && !message.meta?.runId) {
        fallbackIndex = index;
        break;
      }
    }
  }
  if (fallbackIndex < 0) return messages;

  return messages.map((message, index) => index === fallbackIndex
    ? { ...message, meta: { ...message.meta, runId } }
    : message);
}
