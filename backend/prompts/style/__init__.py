"""Default response style."""

from backend.prompts.models import PromptInputs, PromptSection


def build(_: PromptInputs) -> tuple[PromptSection, ...]:
    return (
        PromptSection(
            name="style",
            content="Use concise, direct language. Prefer concrete actions and verified facts.",
            channel="system",
            authority="platform",
            volatility="static",
            instruction_mode="instruction",
            source="backend.prompts.style",
        ),
    )
