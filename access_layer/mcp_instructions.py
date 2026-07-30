"""Track MCP instruction changes already delivered to each conversation session."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class McpInstructionsDeltaState:
    """Names of MCP instruction blocks already announced to one session."""

    announced: set[str] = field(default_factory=set)


class McpInstructionsDeltaRegistry:
    """Emit only MCP instruction additions and removals since the prior run."""

    def __init__(self) -> None:
        """初始化对象依赖和内部状态。"""
        self._states: dict[str, McpInstructionsDeltaState] = {}

    def delta(self, session_id: str, current: dict[str, str]) -> dict[str, list[str]]:
        """计算 MCP 指令相对上次状态的新增和移除内容。"""
        state = self._states.setdefault(session_id, McpInstructionsDeltaState())
        current_names = set(current)
        added = sorted(current_names - state.announced)
        removed = sorted(state.announced - current_names)
        state.announced.difference_update(removed)
        state.announced.update(added)
        return {
            "addedNames": added,
            "addedBlocks": [f"## {name}\n{current[name]}" for name in added],
            "removedNames": removed,
        }

    def clear(self, session_id: str | None = None) -> None:
        """清空当前对象维护的缓存或会话状态。"""
        if session_id is None:
            self._states.clear()
        else:
            self._states.pop(session_id, None)


MCP_INSTRUCTIONS_DELTAS = McpInstructionsDeltaRegistry()
