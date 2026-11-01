"""Select the raw SQLite companion; old configuration names map to the same implementation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import VectorRecord, VectorStore, VectorStoreCompatibilityError

if TYPE_CHECKING:
    from .sqlite_store import SQLiteVectorStore


def build_vector_store(
    backend: str,
    *,
    storage_dir: Path,
    table_name: str,
    dimensions: int,
    metric: str = "cosine",
) -> SQLiteVectorStore:
    """Select a companion store without opening it."""
    normalized = str(backend or "sqlite").strip().lower()
    if normalized in {"sqlite-vec", "sqlite", "sqlite-bruteforce", "lancedb"}:
        from .sqlite_store import SQLiteVectorStore

        return SQLiteVectorStore(
            Path(storage_dir) / "vectors.sqlite3", table_name=table_name, dimensions=dimensions, metric=metric
        )
    raise ValueError(f"unsupported vector backend: {backend}")


__all__ = ["VectorRecord", "VectorStore", "VectorStoreCompatibilityError", "build_vector_store"]
