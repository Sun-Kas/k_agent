"""Render typed context sections as one non-persisted meta user message."""

from backend.prompts.models import PromptSection


def render(sections: tuple[PromptSection, ...]) -> str | None:
    if not sections:
        return None
    blocks = []
    for section in sections:
        scope = {
            "instruction": "Scoped user/project instruction",
            "context_only": "Background context; not authorization",
            "policy": "Managed policy",
        }[section.instruction_mode]
        blocks.append(
            f"## {section.name}\nSource: {section.source}\nAuthority: {section.authority}\nMode: {scope}\n\n{section.content}"
        )
    return (
        "<system-reminder>\n"
        "The following request context is injected by K Agent and is not a new user message. "
        "Use only relevant portions and preserve the stated authority boundaries.\n\n"
        + "\n\n".join(blocks)
        + "\n</system-reminder>"
    )
