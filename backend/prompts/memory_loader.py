"""Compatibility wrapper for the memory subsystem.

New code should import from backend.memory. This module remains so older imports
inside the app keep working while the backend is migrated.
"""

from backend.memory import (
    MemoryFile,
    MemoryType,
    append_auto_memory,
    get_auto_memory_entrypoint,
    get_memory_context,
    get_memory_files,
    get_nested_memory_files,
    read_auto_memory,
    search_auto_memory,
)

__all__ = [
    "MemoryFile",
    "MemoryType",
    "append_auto_memory",
    "get_auto_memory_entrypoint",
    "get_memory_context",
    "get_memory_files",
    "get_nested_memory_files",
    "read_auto_memory",
    "search_auto_memory",
]
