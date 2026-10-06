"""misc 领域工具；类型化业务结果在此产生。"""

from __future__ import annotations
from backend.tools.contracts import ToolExecutionContext
from typing import Any
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool
from backend.tools.contracts import ToolOutcome, json_outcome
from datetime import datetime, timezone


async def get_current_time(ctx: ToolExecutionContext, _: dict[str, Any]) -> ToolOutcome:
    """返回 UTC ISO 时间，供模型校准「现在」。"""
    return json_outcome({"now": datetime.now(timezone.utc).isoformat()})


async def echo_text(ctx: ToolExecutionContext, payload: dict[str, Any]) -> ToolOutcome:
    """返回输入文本用于工具链路测试。"""
    return json_outcome({"echoed": str(payload.get("text", ""))})


GET_CURRENT_TIME_TOOL = define_tool(
    name="get_current_time",
    description="Get the current local server time in ISO format.",
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    execute=get_current_time,
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
)

ECHO_TEXT_TOOL = define_tool(
    name="echo_text",
    description="Echo user-provided text for testing the tool pipeline.",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to echo back",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    execute=echo_text,
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
)

MISC_TOOL_FACTORIES = (
    GET_CURRENT_TIME_TOOL,
    ECHO_TEXT_TOOL,
)
