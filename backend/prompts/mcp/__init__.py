"""MCP initialize text is external context, never platform policy."""

from backend.prompts.models import PromptInputs, PromptSection


def build(inputs: PromptInputs) -> tuple[PromptSection, ...]:
    return tuple(
        PromptSection(
            name=f"mcp_instruction_{index}",
            content=item.content.strip(),
            channel="context",
            authority="external",
            volatility="request",
            instruction_mode="context_only",
            source=f"mcp:{item.server_id}",
            sensitive=True,
        )
        for index, item in enumerate(inputs.mcp_instructions)
        if item.content.strip()
    )
