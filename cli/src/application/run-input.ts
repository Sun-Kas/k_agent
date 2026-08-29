import { randomUUID } from "node:crypto";
import type { CliRuntimeConfig } from "../config/index.js";
import type { AgUiRunInput, ApprovalActivity, UserQuestionAnswers } from "../protocol/index.js";

export interface NewRun {
  input: AgUiRunInput;
  sessionId: string;
  runId: string;
  userMessageId: string;
}

export function createRunInput(prompt: string, config: CliRuntimeConfig, sessionId?: string): NewRun {
  const targetSessionId = sessionId ?? randomUUID();
  const runId = randomUUID();
  const userMessageId = randomUUID();
  return {
    sessionId: targetSessionId,
    runId,
    userMessageId,
    input: {
      threadId: targetSessionId,
      runId,
      state: {},
      messages: [{ id: userMessageId, role: "user", content: prompt }],
      tools: [],
      context: [],
      forwardedProps: forwardedProps(config),
    },
  };
}

/**
 * HITL 恢复必须创建新 run，并且 messages 为空。问题、工具参数和 request hash
 * 仍由 Access Layer 持久 checkpoint 提供，CLI 只能提交用户的决策或答案。
 */
export function createResumeInput(
  approval: ApprovalActivity,
  config: CliRuntimeConfig,
  decision: { action: "approve" | "deny" | "cancel"; scope?: "once" | "run" }
    | { action: "answer"; answers: UserQuestionAnswers },
): AgUiRunInput {
  const reconfirm = approval.status === "unknown_outcome" || approval.status === "resume_failed";
  return {
    threadId: approval.threadId,
    runId: randomUUID(),
    state: {},
    messages: [],
    tools: [],
    context: [],
    resume: [{
      interruptId: approval.id,
      status: decision.action === "cancel" ? "cancelled" : "resolved",
      ...(decision.action === "cancel" ? {} : {
        payload: decision.action === "answer"
          ? { answers: decision.answers, ...(reconfirm ? { reconfirm: true } : {}) }
          : {
            approved: decision.action === "approve",
            scope: decision.scope ?? "once",
            ...(reconfirm ? { reconfirm: true } : {}),
          },
      }),
    }],
    forwardedProps: forwardedProps(config),
  };
}

function forwardedProps(config: CliRuntimeConfig): AgUiRunInput["forwardedProps"] {
  return {
    ...(config.modelId ? { modelId: config.modelId } : {}),
    mcpServerIds: config.mcpServerIds,
    skillIds: config.skillIds,
    reasoningEffort: config.reasoningEffort,
    agentKind: config.agentKind,
    agentOptions: {
      permissionMode: config.permissionMode,
      ...(config.agentKind === "k_agent" ? {} : { cliSessionMode: config.cliSessionMode }),
    },
  };
}
