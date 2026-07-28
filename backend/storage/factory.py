from __future__ import annotations

from backend.config import Settings
from backend.storage.file import FileStorage
from backend.storage.interface import StorageBackend


def create_storage(settings: Settings) -> StorageBackend:
    """Create the configured storage backend.

    Keep callers behind StorageBackend so switching to Redis/MySQL/ES is a
    factory change plus a new implementation, not a rewrite of session/memory
    code.
    """
    if settings.storage_backend == "file":
        return FileStorage(settings.storage_base_dir)
    raise ValueError(f"Unsupported storage backend: {settings.storage_backend}")
