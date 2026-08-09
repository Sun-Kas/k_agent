"""Run-scoped prompt guidance for spoken assistant responses."""

from __future__ import annotations

from typing import Any, Mapping


VOICE_CONVERSATION_SYSTEM_PROMPT = """
This turn is part of a real-time voice conversation. Respond in natural,
conversational language that sounds clear when read aloud. Prefer concise
sentences and smooth transitions. Avoid unnecessary headings, tables, dense
lists, code blocks, raw URLs, and heavy Markdown unless the user explicitly
asks for them or they are required for an accurate answer. Do not mention
these style instructions in the response.
""".strip()


VOICE_STYLE_PROMPTS = {
    "natural": "Use an even, natural tone with clear everyday phrasing.",
    "warm": "Sound warm, patient, and considerate without becoming verbose or overly familiar.",
    "lively": "Sound energetic and clear, using brisk phrasing while preserving accuracy.",
    "professional": "Be direct, composed, and concise; lead with the most useful conclusion.",
    "storytelling": "Use flowing spoken transitions and concrete imagery when explaining, without inventing facts.",
}


def voice_conversation_prompt(options: Mapping[str, Any]) -> str | None:
    """Return voice guidance only for an explicitly enabled conversation turn."""

    if options.get("voiceConversation") is not True:
        return None
    raw_style = options.get("voiceStyle")
    # Only fixed style IDs cross the browser/backend trust boundary. Unknown
    # values fall back instead of becoming user-controlled system prompt text.
    style = raw_style if isinstance(raw_style, str) and raw_style in VOICE_STYLE_PROMPTS else "natural"
    return f"{VOICE_CONVERSATION_SYSTEM_PROMPT}\n\nVoice style: {VOICE_STYLE_PROMPTS[style]}"
