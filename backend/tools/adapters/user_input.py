"""用户输入通过 durable Interrupt 挂起，Resume 只产生 Observation。"""

from backend.tools import ToolBinding
from backend.tools.contracts import ToolExecutionContext
import json
from dataclasses import replace
from backend.approvals import canonical_json_sha256
from backend.permissions import PermissionDecision
from backend.tools.contracts import ToolOutcome, ToolOutcomeStatus, json_outcome, thaw
from backend.user_questions import (
    normalize_user_questions,
    normalize_user_question_answers,
    render_user_question_result,
)


def bind_user_input(factory)->ToolBinding:
    """AskUserQuestion 不执行副作用：prepare 发提问卡并挂起本轮 HTTP，resume 把答案写成工具结果。"""

    binding = factory()

    async def prepare(runtime, current):
        # 模型一要提问就走 HITL：把 questions 塞进当前流的 Interrupt，然后等 stream 结束。
        # 不会调用 execute。用户点选发生在另一次 Resume 请求。
        args = thaw(current.arguments)
        questions = normalize_user_questions(args)
        handler = runtime.get("approval_handler")
        if handler is None:
            raise RuntimeError("AskUserQuestion requires an interactive client")
        await handler(
            binding.spec.provider_name,
            PermissionDecision("ask", "需要用户提供信息后才能继续。"),
            {
                "toolName": current.requested_name,
                "callId": current.call_id,
                "iteration": current.iteration,
                "arguments": args,
                "questions": questions,
                "source": "user_input",
            },
        )
        raise RuntimeError("AskUserQuestion interrupt closed unexpectedly")

    def resume(runtime, name, arguments, decision, payload):
        digest = canonical_json_sha256(
            {
                "target": name,
                "source": "user_input",
                "serverId": None,
                "arguments": arguments,
            }
        )
        if digest != runtime.get("resume_request_hash"):
            raise RuntimeError("User input resume does not match the interrupted call")
        if decision.get("status") == "cancelled":
            return ToolOutcome.failed(
                code="UserInputCancelled",
                message="User input cancelled",
                content=json.dumps(
                    {"ok": False, "cancelled": True}, ensure_ascii=False
                ),
                status=ToolOutcomeStatus.CANCELLED,
            )
        if not isinstance(payload, dict):
            raise RuntimeError("User input resume payload is missing")
        questions = normalize_user_questions(arguments)
        answers = normalize_user_question_answers(questions, payload.get("answers"))
        return json_outcome(render_user_question_result(questions, answers))

    # frozen ToolBinding 不能原地改；复制一份，只换上提问用的 prepare/resume，spec/execute 仍是原工具。
    return replace(binding, prepare=prepare, resume=resume)


from typing import Any
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool


async def _unreachable_execute(
    ctx: ToolExecutionContext, _payload: dict[str, Any]
) -> ToolOutcome:
    # define_tool 必须有 execute；提问只走 prepare/resume，正常路径到不了这里。
    raise RuntimeError("AskUserQuestion must be handled by the HITL interrupt path")


ASK_USER_QUESTION_TOOL = define_tool(
    name="AskUserQuestion",
    description=(
        "Ask the user one to four clarification questions when their answer is required "
        "before continuing. Provide 2-4 useful preset options for each question. The UI "
        "also lets the user add free text, either instead of or in addition to selections."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {
                            "type": "string",
                            "description": "Short label, at most 24 characters.",
                        },
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label", "description"],
                                "additionalProperties": False,
                            },
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "Whether multiple preset options may be selected.",
                            "default": False,
                        },
                    },
                    "required": ["header", "question", "options"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
    execute=_unreachable_execute,
    execution_policy=ToolExecutionPolicy("control", supports_live_output=False),
)
