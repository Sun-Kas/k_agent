"""Pure validation helpers for durable ``AskUserQuestion`` interrupts.

The browser may submit answers, but the question definition comes only from the
server-side checkpoint.  Keeping normalization here lets the Agent Backend and
Access Layer enforce the same contract without trusting rendered form state.
"""

from __future__ import annotations

from typing import Any


MAX_QUESTIONS = 4
MAX_OPTIONS = 4
MAX_CUSTOM_ANSWER_CHARS = 4_000


def normalize_user_questions(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate model arguments and add stable per-call question identifiers.

    Example — checkpoint ``raw_arguments`` (model output, no ids yet)::

        {
          "questions": [{
            "header": "实现方式",
            "question": "你希望怎样继续？",
            "multiSelect": True,
            "options": [
              {"label": "方案 A", "description": "保持当前边界"},
              {"label": "方案 B", "description": "扩大范围"},
              {"label": "方案 C", "description": "先做最小版本"}
            ]
          }]
        }

    Returns questions with ``id="question-1"`` (and so on).  Later answer
    keys must use these ids, not browser-invented ones.
    """

    raw_questions = arguments.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= MAX_QUESTIONS:
        raise ValueError("AskUserQuestion requires between 1 and 4 questions.")

    normalized: list[dict[str, Any]] = []
    for index, raw_question in enumerate(raw_questions, start=1):
        if not isinstance(raw_question, dict):
            raise ValueError(f"Question {index} must be an object.")
        unknown = set(raw_question) - {"header", "question", "options", "multiSelect"}
        if unknown:
            raise ValueError(
                f"Question {index} contains unsupported fields: {', '.join(sorted(unknown))}"
            )
        header = _required_text(raw_question.get("header"), f"Question {index} header")
        question = _required_text(raw_question.get("question"), f"Question {index} text")
        if len(header) > 24:
            raise ValueError(f"Question {index} header must be at most 24 characters.")
        if len(question) > 500:
            raise ValueError(f"Question {index} text must be at most 500 characters.")
        raw_options = raw_question.get("options")
        if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= MAX_OPTIONS:
            raise ValueError(f"Question {index} requires between 2 and 4 options.")
        options: list[dict[str, str]] = []
        labels: set[str] = set()
        for option_index, raw_option in enumerate(raw_options, start=1):
            if not isinstance(raw_option, dict):
                raise ValueError(f"Question {index} option {option_index} must be an object.")
            if set(raw_option) - {"label", "description"}:
                raise ValueError(f"Question {index} option {option_index} has unsupported fields.")
            label = _required_text(
                raw_option.get("label"), f"Question {index} option {option_index} label"
            )
            description = _required_text(
                raw_option.get("description"),
                f"Question {index} option {option_index} description",
            )
            if len(label) > 100 or len(description) > 500:
                raise ValueError(f"Question {index} option {option_index} is too long.")
            if label in labels:
                raise ValueError(f"Question {index} option labels must be unique.")
            labels.add(label)
            options.append({"label": label, "description": description})
        multi_select = raw_question.get("multiSelect", False)
        if not isinstance(multi_select, bool):
            raise ValueError(f"Question {index} multiSelect must be a boolean.")
        normalized.append({
            "id": f"question-{index}",
            "header": header,
            "question": question,
            "options": options,
            "multiSelect": multi_select,
        })
    return normalized


def normalize_user_question_answers(
    questions: list[dict[str, Any]], answers: Any
) -> dict[str, dict[str, Any]]:
    """Validate selected labels plus optional free text for every question.

    Preset selections and custom text are deliberately independent.  A user may
    submit either one, or both together; ``multiSelect`` only limits how many
    preset labels can be selected.

    Example — browser resume payload, keyed by ids from
    ``normalize_user_questions``::

        {
          "question-1": {
            "selected": ["方案 A", "方案 C"],
            "custom": "同时保留旧接口"
          }
        }

    ``selected`` labels must match the checkpoint options.  Unknown labels,
    missing question ids, or both ``selected`` and ``custom`` empty fail.
    Returns the same shape after strip / uniqueness checks.
    """

    if not isinstance(answers, dict):
        raise ValueError("AskUserQuestion answers must be an object.")
    expected_ids = {str(question.get("id") or "") for question in questions}
    if set(answers) != expected_ids:
        raise ValueError("Answers must cover every question exactly once.")

    normalized: dict[str, dict[str, Any]] = {}
    for question in questions:
        question_id = str(question["id"])
        raw_answer = answers.get(question_id)
        if not isinstance(raw_answer, dict) or set(raw_answer) - {"selected", "custom"}:
            raise ValueError(f"Answer for {question_id} is malformed.")
        raw_selected = raw_answer.get("selected", [])
        if not isinstance(raw_selected, list) or any(
            not isinstance(value, str) for value in raw_selected
        ):
            raise ValueError(f"Selected answers for {question_id} must be strings.")
        selected = [value.strip() for value in raw_selected if value.strip()]
        if len(selected) != len(set(selected)):
            raise ValueError(f"Selected answers for {question_id} must be unique.")
        allowed = {
            str(option.get("label") or "") for option in question.get("options", [])
        }
        if any(value not in allowed for value in selected):
            raise ValueError(f"Answer for {question_id} contains an unknown option.")
        if not question.get("multiSelect") and len(selected) > 1:
            raise ValueError(f"Answer for {question_id} allows only one preset option.")
        custom_value = raw_answer.get("custom", "")
        if not isinstance(custom_value, str):
            raise ValueError(f"Custom answer for {question_id} must be a string.")
        custom = custom_value.strip()
        if len(custom) > MAX_CUSTOM_ANSWER_CHARS:
            raise ValueError(f"Custom answer for {question_id} is too long.")
        if not selected and not custom:
            raise ValueError(f"Answer for {question_id} must select an option or add text.")
        normalized[question_id] = {"selected": selected, "custom": custom}
    return normalized


def render_user_question_result(
    questions: list[dict[str, Any]], answers: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Return a compact, model-readable result while preserving both answer modes.

    Example — Observation written back as the AskUserQuestion tool result::

        {
          "ok": True,
          "answers": [{
            "id": "question-1",
            "question": "你希望怎样继续？",
            "selected": ["方案 A", "方案 C"],
            "custom": "同时保留旧接口"
          }]
        }
    """

    return {
        "ok": True,
        "answers": [
            {
                "id": question["id"],
                "question": question["question"],
                "selected": answers[question["id"]]["selected"],
                "custom": answers[question["id"]]["custom"],
            }
            for question in questions
        ],
    }


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required.")
    return value.strip()
