"""Shared filenames, limits, and parsing patterns for the memory subsystem."""

from __future__ import annotations


MEMORY_INSTRUCTION_PROMPT = (
    "Workspace and user instructions are shown below. Be sure to adhere to these "
    "instructions. IMPORTANT: These instructions OVERRIDE any default behavior "
    "and you MUST follow them exactly as written."
)

MAX_INCLUDE_DEPTH = 5
MAX_MEMORY_CHARACTER_COUNT = 40_000
MAX_AUTOMEM_ENTRYPOINT_CHARS = 25_000
MAX_AUTOMEM_ENTRYPOINT_LINES = 200

# Claude-compatible instruction files are the single project-rule convention.
# K Agent's own durable, writable memory remains data/memory/MEMORY.md.
MEMORY_FILENAMES = ("CLAUDE.md",)
LOCAL_MEMORY_FILENAMES = ("CLAUDE.local.md",)
MEMORY_DIR_NAMES = (".claude",)

ALLOWED_INCLUDE_EXTENSIONS = {
    "",
    ".md",
    ".mdx",
    ".txt",
    ".json",
    ".jsonc",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".html",
    ".sh",
}
