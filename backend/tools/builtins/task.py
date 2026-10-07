"""task 领域工具；类型化业务结果在此产生。"""

from __future__ import annotations
from backend.tools.contracts import ToolExecutionContext
from typing import Any
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool
from backend.tools.contracts import ToolOutcome, json_outcome, UpdatePlan, freeze
from backend.tools.builtins.common import _json


async def cc_todo_write(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
    """记录并返回模型提交的任务列表。"""
    todos = payload.get("todos")
    if not isinstance(todos, list):
        return _json({"ok": False, "error": "todos must be a list"})
    normalized = []
    for index, item in enumerate(todos, start=1):
        if isinstance(item, str):
            normalized.append({"id": str(index), "content": item, "status": "pending"})
        elif isinstance(item, dict):
            normalized.append(
                {
                    "id": str(item.get("id") or index),
                    "content": str(item.get("content") or ""),
                    "status": str(item.get("status") or "pending"),
                }
            )
    return json_outcome(
        {"ok": True, "todos": normalized}, effects=(UpdatePlan(freeze(normalized)),)
    )


TODOWRITE_TOOL = define_tool(
    name="TodoWrite",
    description="Create or update the agent's visible todo list for the current task.",
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {"oneOf": [{"type": "object"}, {"type": "string"}]},
            }
        },
        "required": ["todos"],
        "additionalProperties": False,
    },
    execute=cc_todo_write,
    context_policy={"mode": "receipt", "maxResultChars": 12_000},
    execution_policy=ToolExecutionPolicy("control", supports_live_output=False),
)

TASK_TOOL_FACTORIES = (TODOWRITE_TOOL,)
