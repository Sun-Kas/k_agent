"""memory 领域工具；类型化业务结果在此产生。"""

from __future__ import annotations
from backend.tools.contracts import ToolExecutionContext
from typing import Any
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool
from backend.tools.contracts import ToolOutcome, json_outcome
from backend.memory import (
    append_auto_memory,
    compact_auto_memory,
    read_auto_memory,
    search_auto_memory,
)


async def read_personal_memory(
    ctx: ToolExecutionContext, _: dict[str, Any]
) -> ToolOutcome:
    """读取 `$K_AGENT_HOME/content/memory/MEMORY.md`。"""
    path, content = read_auto_memory()
    return json_outcome({"path": str(path), "content": content})


async def append_personal_memory(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
    """向 MEMORY.md 追加一条精简的持久记忆。"""
    text = str(payload.get("text", "")).strip()
    if not text:
        return json_outcome({"ok": False, "error": "text is required"})
    path = append_auto_memory(text)
    return json_outcome({"ok": True, "path": str(path)})


async def search_personal_memory(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
    """在 MEMORY.md 中按子串搜索匹配行。"""
    query = str(payload.get("query", "")).strip().lower()
    path, matches = search_auto_memory(query)
    return json_outcome({"path": str(path), "matches": matches})


async def compact_personal_memory(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
    """去重并裁剪 MEMORY.md，控制持久记忆体积。"""
    max_items = int(payload.get("maxItems", 200))
    path, before, after = compact_auto_memory(max_items=max_items)
    return json_outcome(
        {"ok": True, "path": str(path), "itemsBefore": before, "itemsAfter": after}
    )


READ_PERSONAL_MEMORY_TOOL = define_tool(
    name="read_personal_memory",
    description="Read K Agent's durable memory from $K_AGENT_HOME/content/memory/MEMORY.md.",
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    execute=read_personal_memory,
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
)

APPEND_PERSONAL_MEMORY_TOOL = define_tool(
    name="append_personal_memory",
    description="Append a concise durable memory item to $K_AGENT_HOME/content/memory/MEMORY.md.",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "One concise project-local memory item to remember.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    execute=append_personal_memory,
    execution_policy=ToolExecutionPolicy("write", supports_live_output=False),
)

SEARCH_PERSONAL_MEMORY_TOOL = define_tool(
    name="search_personal_memory",
    description="Search durable memory in $K_AGENT_HOME/content/memory/MEMORY.md.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search text.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    execute=search_personal_memory,
    execution_policy=ToolExecutionPolicy("read", supports_live_output=False),
)

COMPACT_PERSONAL_MEMORY_TOOL = define_tool(
    name="compact_personal_memory",
    description="Deduplicate and trim durable memory in $K_AGENT_HOME/content/memory/MEMORY.md.",
    parameters={
        "type": "object",
        "properties": {
            "maxItems": {
                "type": "integer",
                "description": "Maximum memory items to keep.",
                "default": 200,
            }
        },
        "additionalProperties": False,
    },
    execute=compact_personal_memory,
    execution_policy=ToolExecutionPolicy("write", supports_live_output=False),
)

MEMORY_TOOL_FACTORIES = (
    READ_PERSONAL_MEMORY_TOOL,
    APPEND_PERSONAL_MEMORY_TOOL,
    SEARCH_PERSONAL_MEMORY_TOOL,
    COMPACT_PERSONAL_MEMORY_TOOL,
)
