"""Compatibility name for the in-process raw vector companion."""

from .sqlite_store import SQLiteVectorStore

ProcessLanceVectorStore = SQLiteVectorStore
