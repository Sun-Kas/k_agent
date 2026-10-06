from __future__ import annotations

"""本地沙箱提示的用户侧投影。"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.agent.hooks import AgentRunContext
from backend.config.config import Settings
from backend.sandbox import notice_from_tool_result


def _sandbox_user_notice(
    context: AgentRunContext,
    tool_name: str,
    tool_result: str,
    config: Settings,
) -> str | None:
    """Surface sandbox install/degrade messages once (install outcomes always)."""

    message = notice_from_tool_result(tool_name, tool_result, settings=config)
    if message is None:
        return None
    # Install success/failure should always reach the status pill. Unavailable
    # notices are once per run so repeated Bash calls do not spam the UI.
    if tool_name == "InstallSandbox":
        return message
    if context.metadata.get("sandbox_notice_emitted"):
        return None
    context.metadata["sandbox_notice_emitted"] = True
    return message
