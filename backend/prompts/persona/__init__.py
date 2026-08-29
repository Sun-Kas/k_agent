"""Persona selection below the sealed platform contract."""

from backend.prompts.models import PromptInputs, PromptSection


DEFAULT_PERSONA = """
Be a helpful personal assistant for planning, answering questions, and completing tasks.
Use tools when they help. Keep answers concise, practical, and grounded in available context.
When tool results are returned, base your response on those results.
""".strip()


def build(inputs: PromptInputs) -> tuple[PromptSection, ...]:
    persona = inputs.persona
    if persona.override:
        content = persona.override.strip()
    elif persona.agent and not persona.proactive:
        content = persona.agent.strip()
    elif persona.custom:
        content = persona.custom.strip()
    else:
        content = DEFAULT_PERSONA
    if persona.agent and persona.proactive:
        content = "\n\n".join(item for item in (content, persona.agent.strip()) if item)
    if persona.append:
        content = "\n\n".join(item for item in (content, persona.append.strip()) if item)
    if not content:
        return ()
    return (
        PromptSection(
            name="persona",
            content=content,
            channel="system",
            authority="user",
            volatility="session" if any((persona.custom, persona.agent, persona.append, persona.override)) else "static",
            instruction_mode="instruction",
            source="prompt_inputs.persona",
        ),
    )
