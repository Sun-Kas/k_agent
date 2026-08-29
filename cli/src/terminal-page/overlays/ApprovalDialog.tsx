import React from "react";
import { Text, useInput } from "ink";
import type { ApprovalActivity } from "../../protocol/index.js";
import type { InterruptAnswer } from "../types.js";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { TERMINAL_DESIGN } from "../design.js";
import { Overlay } from "./Overlay.js";

export function ApprovalDialog({ approval, onAnswer, onClose }: {
  approval: ApprovalActivity;
  onAnswer: (answer: InterruptAnswer) => void;
  onClose: () => void;
}): React.ReactElement {
  useInput((input, key) => {
    if (key.escape) return onClose();
    if (approval.status === "submitting") return;
    if (input === "d") onAnswer({ action: "deny" });
    if (input === "a") onAnswer({ action: "approve", scope: "once" });
    if (input === "r") onAnswer({ action: "approve", scope: "run" });
  });
  return (
    <Overlay title={TERMINAL_DESIGN.copy.approvalRequired}>
      <Text color={TERMINAL_DESIGN.colors.warning}>{sanitizeTerminalContent(approval.title)}</Text>
      <Text wrap="wrap">{sanitizeTerminalContent(approval.message)}</Text>
      {approval.status === "unknown_outcome" ? <Text color={TERMINAL_DESIGN.colors.danger}>上次提交结果不确定，再次确认会带 reconfirm 标记。</Text> : null}
      <Text>[d] 拒绝   [a] 允许一次   [r] 本轮允许</Text>
      <Text color={TERMINAL_DESIGN.colors.muted}>Esc 仅返回上下文，不会自动拒绝。</Text>
    </Overlay>
  );
}
