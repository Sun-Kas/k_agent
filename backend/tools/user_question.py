"""Model-facing ``AskUserQuestion`` definition.

Execution is intercepted at the Agent's sealed preflight and converted into a
durable input-required Interrupt.  The executor is intentionally unreachable:
answers are returned as the resumed tool result, never sourced from process
globals or a browser-controlled direct tool endpoint.
"""

from __future__ import annotations

from typing import Any

from backend.tools.local import ToolDefinition


async def _unreachable_execute(_payload: dict[str, Any]) -> str:
    raise RuntimeError("AskUserQuestion must be handled by the HITL interrupt path")


ASK_USER_QUESTION_TOOL = ToolDefinition(
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
)
