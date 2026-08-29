import type { UserQuestion, UserQuestionAnswers } from "./types";

export function userQuestionsFromDetail(detail: Record<string, unknown>): UserQuestion[] {
  const raw = detail.questions;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((value) => {
    if (!value || typeof value !== "object") return [];
    const item = value as Record<string, unknown>;
    const options = Array.isArray(item.options)
      ? item.options.flatMap((option) => {
          if (!option || typeof option !== "object") return [];
          const parsed = option as Record<string, unknown>;
          const label = String(parsed.label ?? "").trim();
          if (!label) return [];
          return [{ label, description: String(parsed.description ?? "") }];
        })
      : [];
    const id = String(item.id ?? "").trim();
    const question = String(item.question ?? "").trim();
    if (!id || !question || options.length < 2) return [];
    return [{
      id,
      header: String(item.header ?? "问题"),
      question,
      options,
      multiSelect: item.multiSelect === true
    }];
  });
}

export function userQuestionAnswersComplete(
  questions: UserQuestion[], answers: UserQuestionAnswers
): boolean {
  return questions.length > 0 && questions.every((question) => {
    const answer = answers[question.id];
    return Boolean(answer && (answer.selected.length > 0 || answer.custom.trim()));
  });
}
