from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from access_layer.sessions.store import ResumeConflictError, SessionStore
from access_layer.scheduled_tasks.models import ScheduledApprovalResumeInput
from access_layer.teams.runtime import TeamRuntime
from backend.agent.contracts import AgentRunRequest
from backend.agent.react_agent import OpenAIAgent
from backend.agui import translate_agent_events
from backend.api.schemas import ChatMessage
from backend.mcp_tool import McpClientManager
from backend.tools.registry import get_all_base_tools
from backend.tools.user_question import ASK_USER_QUESTION_TOOL
from backend.user_questions import (
    normalize_user_question_answers,
    normalize_user_questions,
    render_user_question_result,
)


def question_arguments(*, multi_select: bool = False) -> dict:
    return {
        "questions": [{
            "header": "实现方式",
            "question": "你希望怎样继续？",
            "options": [
                {"label": "方案 A", "description": "保持当前边界"},
                {"label": "方案 B", "description": "扩大实现范围"},
                {"label": "方案 C", "description": "先做最小版本"},
            ],
            "multiSelect": multi_select,
        }]
    }


class UserQuestionContractTests(unittest.TestCase):
    def test_tool_is_registered_in_the_base_catalog(self) -> None:
        self.assertIn("AskUserQuestion", {tool.name for tool in get_all_base_tools()})

    def test_selection_custom_text_and_combination_are_all_valid(self) -> None:
        questions = normalize_user_questions(question_arguments())
        cases = (
            {"selected": ["方案 A"], "custom": ""},
            {"selected": [], "custom": "我有另一种想法"},
            {"selected": ["方案 B"], "custom": "但请保留旧接口"},
        )
        for answer in cases:
            with self.subTest(answer=answer):
                normalized = normalize_user_question_answers(
                    questions, {"question-1": answer}
                )
                result = render_user_question_result(questions, normalized)
                self.assertEqual(result["answers"][0]["selected"], answer["selected"])
                self.assertEqual(result["answers"][0]["custom"], answer["custom"])

    def test_single_select_rejects_multiple_presets_but_multi_select_accepts_them(self) -> None:
        answers = {
            "question-1": {"selected": ["方案 A", "方案 C"], "custom": "补充"}
        }
        with self.assertRaisesRegex(ValueError, "only one preset"):
            normalize_user_question_answers(
                normalize_user_questions(question_arguments()), answers
            )
        normalized = normalize_user_question_answers(
            normalize_user_questions(question_arguments(multi_select=True)), answers
        )
        self.assertEqual(normalized["question-1"]["selected"], ["方案 A", "方案 C"])

    def test_team_and_scheduled_resume_contracts_preserve_structured_answers(self) -> None:
        questions = normalize_user_questions(question_arguments())
        answers = {
            "question-1": {"selected": ["方案 C"], "custom": "先验证再扩展"}
        }
        decision = TeamRuntime._approval_resume_decision(
            {"category": "user_input", "detail": {"questions": questions}},
            approval_id="question-interrupt",
            action="answer",
            scope="once",
            answers=answers,
        )
        self.assertEqual(decision["payload"]["answers"], answers)
        scheduled = ScheduledApprovalResumeInput.model_validate({
            "interruptId": "question-interrupt",
            "action": "answer",
            "answers": answers,
        })
        self.assertEqual(scheduled.answers, answers)
        with self.assertRaisesRegex(ValueError, "do not accept question answers"):
            TeamRuntime._approval_resume_decision(
                {"category": "local_tool", "detail": {}},
                approval_id="permission-interrupt",
                action="answer",
                scope="once",
                answers=answers,
            )


class UserQuestionInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def test_agui_marks_question_interrupt_as_input_required(self) -> None:
        async def events():
            payload = {
                "id": "question-interrupt",
                "threadId": "thread",
                "runId": "run",
                "agentKind": "k_agent",
                "category": "user_input",
                "message": "怎样继续？",
                "toolCallId": "question-call",
                "_checkpoint": {"version": 1, "messages": []},
            }
            yield {"type": "interrupt", "payload": payload}

        translated = [
            event async for event in translate_agent_events(events(), "thread", "run")
        ]
        finished = next(event for event in translated if event.type == "RUN_FINISHED")
        interrupt = finished.outcome.interrupts[0]
        self.assertEqual(interrupt.reason, "input_required")
        self.assertEqual(interrupt.response_schema["required"], ["answers"])

    async def test_full_access_still_requests_user_input_without_executing_tool(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def request_input(target, _decision, detail):
            calls.append((target, detail))
            return {"action": "cancel", "scope": "once"}

        request = AgentRunRequest(
            messages=[ChatMessage(
                id="user", role="user", content="test",
                createdAt=datetime.now(timezone.utc),
            )],
            system_prompt="test",
            user_context={},
            model_config={"model": "test", "apiKey": "test"},
            permission_mode="full_access",
        )
        runtime = await OpenAIAgent().create_runtime(
            request,
            [ASK_USER_QUESTION_TOOL],
            McpClientManager([]),
            approval_handler=request_input,
        )
        result = await OpenAIAgent()._run_tool(
            runtime=runtime,
            iteration=0,
            call_id="question-call",
            tool_name="AskUserQuestion",
            arguments=question_arguments(),
        )
        self.assertEqual(calls[0][0], "AskUserQuestion")
        self.assertEqual(calls[0][1]["source"], "user_input")
        self.assertEqual(calls[0][1]["questions"][0]["id"], "question-1")
        self.assertFalse(json.loads(result)["ok"])

    async def test_session_resume_validates_and_persists_answered_status(self) -> None:
        store = SessionStore()
        session = await store.create_session(session_id="question-session")
        questions = normalize_user_questions(question_arguments())
        await store.persist_interrupt(session.id, {
            "type": "ACTIVITY_SNAPSHOT",
            "content": {
                "id": "question-interrupt",
                "threadId": session.id,
                "runId": "run-1",
                "agentKind": "k_agent",
                "category": "user_input",
                "detail": {"questions": questions},
                "requestHash": "sha256:question",
                "_checkpoint": {"version": 1, "kind": "react_tool_boundary"},
            },
        })
        with self.assertRaises(ResumeConflictError):
            await store.prepare_resume(
                session.id,
                [{"interruptId": "question-interrupt", "status": "resolved", "payload": {"answers": {}}}],
                resume_run_id="run-invalid",
            )
        records = await store.prepare_resume(
            session.id,
            [{
                "interruptId": "question-interrupt",
                "status": "resolved",
                "payload": {"answers": {
                    "question-1": {"selected": ["方案 A"], "custom": "并补充说明"}
                }},
            }],
            resume_run_id="run-2",
        )
        self.assertEqual(
            records[0]["decision"]["payload"]["answers"]["question-1"]["custom"],
            "并补充说明",
        )
        await store.finish_resume(session.id, ["question-interrupt"], succeeded=True)
        loaded = await store.get(session.id)
        snapshot = loaded.events[-1]
        self.assertEqual(snapshot["content"]["status"], "answered")
        self.assertEqual(
            snapshot["content"]["answers"]["question-1"]["selected"], ["方案 A"]
        )


if __name__ == "__main__":
    unittest.main()
