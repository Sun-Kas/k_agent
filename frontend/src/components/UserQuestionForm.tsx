import { useMemo, useState } from "react";

import type { UserQuestion, UserQuestionAnswers } from "../types";
import { userQuestionAnswersComplete } from "../user-question";

export function UserQuestionForm({
  questions,
  disabled = false,
  resolvedAnswers,
  onSubmit,
  onCancel
}: {
  questions: UserQuestion[];
  disabled?: boolean;
  resolvedAnswers?: UserQuestionAnswers;
  onSubmit: (answers: UserQuestionAnswers) => Promise<void>;
  onCancel: () => Promise<void>;
}) {
  const initial = useMemo(() => Object.fromEntries(questions.map((question) => [
    question.id,
    resolvedAnswers?.[question.id] ?? { selected: [], custom: "" }
  ])), [questions, resolvedAnswers]);
  const [answers, setAnswers] = useState<UserQuestionAnswers>(initial);
  const readonly = Boolean(resolvedAnswers);
  const complete = userQuestionAnswersComplete(questions, answers);

  function toggle(question: UserQuestion, label: string) {
    if (readonly || disabled) return;
    setAnswers((current) => {
      const previous = current[question.id] ?? { selected: [], custom: "" };
      const selected = previous.selected.includes(label)
        ? previous.selected.filter((value) => value !== label)
        : question.multiSelect ? [...previous.selected, label] : [label];
      return { ...current, [question.id]: { ...previous, selected } };
    });
  }

  return <div className="user-question-form">
    {questions.map((question) => {
      const answer = answers[question.id] ?? { selected: [], custom: "" };
      return <fieldset disabled={disabled || readonly} key={question.id}>
        <legend><small>{question.header}</small><strong>{question.question}</strong></legend>
        <div className="user-question-options">
          {question.options.map((option) => {
            const selected = answer.selected.includes(option.label);
            return <button
              aria-pressed={selected}
              className={selected ? "selected" : ""}
              key={option.label}
              onClick={() => toggle(question, option.label)}
              type="button"
            ><span>{question.multiSelect ? (selected ? "✓" : "□") : (selected ? "●" : "○")}</span><div><strong>{option.label}</strong><small>{option.description}</small></div></button>;
          })}
        </div>
        <label className="user-question-custom">
          <span>补充或自定义输入</span>
          <textarea
            maxLength={4000}
            onChange={(event) => setAnswers((current) => ({
              ...current,
              [question.id]: {
                ...(current[question.id] ?? { selected: [], custom: "" }),
                custom: event.target.value
              }
            }))}
            placeholder="可以只填写这里，也可以在选择选项后补充说明"
            value={answer.custom}
          />
        </label>
      </fieldset>;
    })}
    {!readonly && <footer>
      <button disabled={disabled} onClick={() => void onCancel()} type="button">取消</button>
      <button className="primary" disabled={disabled || !complete} onClick={() => void onSubmit(answers)} type="button">提交回答</button>
    </footer>}
  </div>;
}
