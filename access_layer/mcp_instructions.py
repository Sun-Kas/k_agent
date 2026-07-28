from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class McpInstructionsDeltaState:
    announced: set[str] = field(default_factory=set)


class McpInstructionsDeltaRegistry:
    def __init__(self) -> None:
        self._states: dict[str, McpInstructionsDeltaState] = {}

    def delta(self, session_id: str, current: dict[str, str]) -> dict[str, list[str]]:
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
        if session_id is None:
            self._states.clear()
        else:
            self._states.pop(session_id, None)


MCP_INSTRUCTIONS_DELTAS = McpInstructionsDeltaRegistry()
