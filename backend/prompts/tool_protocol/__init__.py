"""General rules for provider tool calls."""

from backend.prompts.models import PromptInputs, PromptSection


def build(_: PromptInputs) -> tuple[PromptSection, ...]:
    return (
        PromptSection(
            name="tool_protocol",
            content=(
                "Use tools when they materially improve correctness or complete requested actions. "
                "Follow each tool's schema exactly. Treat failed tool results as recoverable observations: "
                "adjust the call or explain the limitation instead of inventing success."
            ),
            channel="system",
            authority="platform",
            volatility="static",
            instruction_mode="policy",
            source="backend.prompts.tool_protocol",
        ),
    )
