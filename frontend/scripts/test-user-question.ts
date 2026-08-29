import assert from "node:assert/strict";

import { userQuestionAnswersComplete, userQuestionsFromDetail } from "../src/user-question";

const questions = userQuestionsFromDetail({
  questions: [{
    id: "question-1",
    header: "实现方式",
    question: "怎样继续？",
    multiSelect: false,
    options: [
      { label: "A", description: "选择 A" },
      { label: "B", description: "选择 B" },
      { label: "C", description: "选择 C" }
    ]
  }]
});

assert.equal(questions.length, 1);
assert.equal(userQuestionAnswersComplete(questions, {
  "question-1": { selected: ["A"], custom: "" }
}), true, "preset-only answers must be submittable");
assert.equal(userQuestionAnswersComplete(questions, {
  "question-1": { selected: [], custom: "我的自定义回答" }
}), true, "custom-only answers must be submittable");
assert.equal(userQuestionAnswersComplete(questions, {
  "question-1": { selected: ["B"], custom: "额外补充" }
}), true, "preset plus custom text must be submittable");
assert.equal(userQuestionAnswersComplete(questions, {
  "question-1": { selected: [], custom: "  " }
}), false, "blank answers must remain disabled");
assert.deepEqual(userQuestionsFromDetail({ questions: [{ id: "broken" }] }), []);

console.log("user question tests passed");
