import React, { useMemo, useRef, useState } from "react";
import { Box, Text, useInput, type DOMElement } from "ink";
import type { ApprovalActivity, UserQuestion, UserQuestionAnswers } from "../../protocol/index.js";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { TERMINAL_DESIGN } from "../design.js";
import { Overlay } from "./Overlay.js";
import { useImeCursor } from "../use-ime-cursor.js";

export function UserQuestionDialog({ approval, onSubmit, onClose }: {
  approval: ApprovalActivity;
  onSubmit: (answers: UserQuestionAnswers) => void;
  onClose: () => void;
}): React.ReactElement {
  const questions = useMemo(() => extractQuestions(approval.detail), [approval.detail]);
  const [questionIndex, setQuestionIndex] = useState(0);
  const [optionIndex, setOptionIndex] = useState(0);
  const [answers, setAnswers] = useState<UserQuestionAnswers>({});
  const [selectedLabels, setSelectedLabels] = useState<string[]>([]);
  const [custom, setCustom] = useState("");
  const customLineRef = useRef<DOMElement | null>(null);
  const question = questions[questionIndex];
  const customPrefix = "> 补充说明：";
  useImeCursor(
    customLineRef,
    `${customPrefix}${sanitizeTerminalContent(custom)}`,
    Boolean(question) && optionIndex === question?.options.length,
  );

  useInput((input, key) => {
    if (key.escape) return onClose();
    if (!question) return;
    if (key.upArrow) return setOptionIndex((value) => Math.max(0, value - 1));
    if (key.downArrow) return setOptionIndex((value) => Math.min(question.options.length, value + 1));
    if ((key.backspace || key.delete) && optionIndex === question.options.length) {
      return setCustom((value) => Array.from(value).slice(0, -1).join(""));
    }
    if (input === " " && optionIndex < question.options.length) {
      const label = question.options[optionIndex]!.label;
      setSelectedLabels((current) => question.multiSelect
        ? current.includes(label) ? current.filter((item) => item !== label) : [...current, label]
        : [label]);
      return;
    }
    if (key.return) {
      const selected = selectedLabels.length
        ? selectedLabels
        : optionIndex < question.options.length ? [question.options[optionIndex]!.label] : [];
      const next = { ...answers, [question.id]: { selected, custom } };
      if (questionIndex < questions.length - 1) {
        setAnswers(next);
        setQuestionIndex((value) => value + 1);
        setOptionIndex(0);
        setSelectedLabels([]);
        setCustom("");
      } else onSubmit(next);
      return;
    }
    if (optionIndex === question.options.length && !key.ctrl && !key.meta && input) {
      setCustom((value) => `${value}${input}`);
    }
  });

  return (
    <Overlay title={TERMINAL_DESIGN.copy.questionRequired}>
      {!question ? <Text color={TERMINAL_DESIGN.colors.danger}>服务端问题结构无法识别，请在 Web 端恢复该 Session。</Text> : (
        <>
          <Text bold>{sanitizeTerminalContent(question.header)}</Text>
          <Text wrap="wrap">{sanitizeTerminalContent(question.question)}</Text>
          {question.options.map((option, index) => (
            <Text key={option.label} inverse={index === optionIndex}>
              {question.multiSelect
                ? `[${selectedLabels.includes(option.label) ? "×" : " "}]`
                : `(${selectedLabels.includes(option.label) || (!selectedLabels.length && index === optionIndex) ? "●" : " "})`} {sanitizeTerminalContent(option.label)}  <Text color={TERMINAL_DESIGN.colors.muted}>{sanitizeTerminalContent(option.description)}</Text>
            </Text>
          ))}
          <Box ref={customLineRef} width="100%">
            <Text inverse={optionIndex === question.options.length}>{customPrefix}{sanitizeTerminalContent(custom)}</Text>
          </Box>
          <Text color={TERMINAL_DESIGN.colors.muted}>↑↓ 移动 · Space 选择 · 输入补充说明 · Enter 下一项/提交 · Esc 暂不回答</Text>
        </>
      )}
    </Overlay>
  );
}

function extractQuestions(detail: Record<string, unknown>): UserQuestion[] {
  const source = Array.isArray(detail.questions) ? detail.questions : [];
  return source.flatMap((value, index) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const record = value as Record<string, unknown>;
    const options = Array.isArray(record.options) ? record.options.flatMap((option) => {
      if (!option || typeof option !== "object" || Array.isArray(option)) return [];
      const item = option as Record<string, unknown>;
      return [{ label: String(item.label ?? ""), description: String(item.description ?? "") }];
    }) : [];
    return [{
      id: String(record.id ?? `question-${index + 1}`),
      header: String(record.header ?? "问题"),
      question: String(record.question ?? ""),
      options,
      multiSelect: Boolean(record.multiSelect ?? record.multi_select),
    }];
  });
}
