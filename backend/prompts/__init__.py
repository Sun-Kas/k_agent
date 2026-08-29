"""Typed K Agent prompt compiler."""

from backend.prompts.compose import compose_prompt
from backend.prompts.lifecycle import prompt_lifecycle_state, reset_prompt_caches
from backend.prompts.models import (
    McpInstruction,
    PersonaInputs,
    PromptBundle,
    PromptInputs,
    PromptSection,
)
from backend.prompts.persona import DEFAULT_PERSONA
# Transitional helpers are kept for existing integrations and tests. The K
# Agent production path imports only ``compose_prompt`` and typed models.
from backend.prompts.prompting import (
    build_effective_system_prompt,
    build_nested_memory_context,
    build_prompt_bundle,
    classify_paths_for_memory,
    extract_paths_from_value,
    extract_referenced_paths,
)
from backend.prompts.voice_prompt import (
    VOICE_CONVERSATION_SYSTEM_PROMPT,
    voice_conversation_prompt,
)

__all__ = [
    "compose_prompt",
    "McpInstruction",
    "PersonaInputs",
    "PromptBundle",
    "PromptInputs",
    "PromptSection",
    "DEFAULT_PERSONA",
    "build_effective_system_prompt",
    "build_nested_memory_context",
    "build_prompt_bundle",
    "classify_paths_for_memory",
    "extract_paths_from_value",
    "extract_referenced_paths",
    "VOICE_CONVERSATION_SYSTEM_PROMPT",
    "voice_conversation_prompt",
    "prompt_lifecycle_state",
    "reset_prompt_caches",
]
