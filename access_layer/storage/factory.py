"""Storage backend factory selected from Access Layer settings."""

from __future__ import annotations

from access_layer.settings import Settings
from access_layer.storage.file import FileStorage
from access_layer.storage.interface import StorageBackend


def create_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "file":
        return FileStorage(settings.storage_base_dir)
    raise ValueError(f"Unsupported storage backend: {settings.storage_backend}")
