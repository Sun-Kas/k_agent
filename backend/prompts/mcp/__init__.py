"""MCP initialize `instructions` as external context, never platform policy.

Protocol: optional string on InitializeResult. Clients MAY inject it as a
model hint. We keep it in the context channel because the server wrote it.
"""

from backend.prompts.models import PromptInputs, PromptSection


def build(inputs: PromptInputs) -> tuple[PromptSection, ...]:
    # One section per server that actually returned handshake instructions.
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
