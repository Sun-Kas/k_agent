"""Short guidance conditional on the final provider-visible catalog."""

from backend.prompts.models import PromptInputs, PromptSection


def build(inputs: PromptInputs) -> tuple[PromptSection, ...]:
    guidance: list[str] = []
    if inputs.tool_catalog.has("AskUserQuestion"):
        guidance.append(
            "When asking for a decision, offer concise options and permit custom input; a response may contain both selected options and custom text."
        )
    if inputs.tool_catalog.has("Skill"):
        guidance.append(
            "Use the `Skill` tool when the request context lists a skill that matches the task; load the body only when needed."
        )
    if not guidance:
        return ()
    return (
        PromptSection(
            name="tool_guidance",
            content="\n".join(f"- {item}" for item in guidance),
            channel="system",
            authority="platform",
            volatility="request",
            instruction_mode="policy",
            source="final_tool_catalog",
        ),
    )
