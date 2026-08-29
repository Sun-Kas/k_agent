"""K Agent product identity."""

from backend.prompts.models import PromptInputs, PromptSection


def build(_: PromptInputs) -> tuple[PromptSection, ...]:
    return (
        PromptSection(
            name="identity",
            content="You are K Agent, an AI assistant that can reason, use tools, and collaborate with the user.",
            channel="system",
            authority="platform",
            volatility="static",
            instruction_mode="policy",
            source="backend.prompts.identity",
        ),
    )
