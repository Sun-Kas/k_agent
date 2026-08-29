"""Trusted request policy that may vary per run."""

from backend.prompts.models import PromptInputs, PromptSection
from backend.prompts.voice import voice_conversation_prompt


def build(inputs: PromptInputs) -> tuple[PromptSection, ...]:
    parts = [f"Runtime permission mode: {inputs.permission_mode}."]
    if inputs.output_workspace is not None:
        kind = "Team task" if inputs.team_id else "session"
        parts.append(
            f"The output workspace for this {kind} is `{inputs.output_workspace}`. "
            "Write requested deliverables inside it; relative output paths resolve there. "
            "This output workspace is not the project instruction root."
        )
        if inputs.team_id:
            parts.append("Place formal Team deliverables under its `output/` directory.")
    voice = voice_conversation_prompt(inputs.options)
    if voice:
        parts.append(voice)
    if inputs.cache_breaker:
        parts.append(inputs.cache_breaker)
    return (
        PromptSection(
            name="runtime_policy",
            content="\n\n".join(parts),
            channel="system",
            authority="platform",
            volatility="request",
            instruction_mode="policy",
            source="prompt_inputs.runtime",
            sensitive=bool(inputs.output_workspace),
        ),
    )
