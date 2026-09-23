"""Rebuildable vector companions: the Lance store, its process-isolated driver, the SQLite brute-force fallback, and compaction.

SQLite is the sole authority for facts; every store here is a derived index
that can be rebuilt from it.  ``VectorStore`` states once what the runtime asks
of a companion, and ``store.build_vector_store`` picks the implementation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class VectorRecord:
    id: str
    scope_id: str
    source: str
    target: str
    content: str
    summary: str
    updated_at: str
    vector: list[float]


class VectorStoreCompatibilityError(RuntimeError):
    """An existing vector table cannot be opened with the requested schema.

    Deliberately non-destructive: a caller that needs another schema or
    embedding space builds a new generation explicitly instead of replacing
    the active table during startup.
    """


class VectorStore(ABC):
    """One vector companion table.

    ``open`` may create the companion; ``open_existing`` never creates
    anything.  Rows are plain dicts shaped like :class:`VectorRecord`, and
    ``search`` returns them with a ``_distance`` for the requested metric.
    """

    backend: str

    def __init__(self, db_path: Path, *, table_name: str, dimensions: int, metric: str = "cosine") -> None:
        self.db_path = Path(db_path)
        self.table_name = table_name
        self.dimensions = int(dimensions)
        self.metric = (metric or "cosine").strip().lower()

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def open_existing(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None: ...

    @abstractmethod
    def delete_by_ids(self, ids: list[str]) -> None: ...

    @abstractmethod
    def contains_id(self, memory_id: str) -> bool: ...

    @abstractmethod
    def list_ids(self) -> list[str]: ...

    @abstractmethod
    def list_records(self) -> dict[str, dict[str, Any]]: ...

    @abstractmethod
    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]: ...

    def search_scopes(self, vector: list[float], *, scope_ids: Iterable[str], limit: int) -> list[dict[str, Any]]:
        """The ``limit`` nearest rows across several partitions, nearest first.

        This default asks one partition at a time; a store that can filter by a list overrides it
        with one request, which is what keeps an entry holding a hundred scopes inside its budget.
        """
        rows = [row for scope_id in dict.fromkeys(scope_ids) for row in self.search(vector, scope_id=scope_id, limit=limit)]
        rows.sort(key=lambda row: float(row.get("_distance") or 0.0))
        return rows[: max(0, int(limit))]

    @abstractmethod
    def count_rows(self) -> int: ...

    def upsert(self, record: VectorRecord | Mapping[str, Any]) -> None:
        self.upsert_records([asdict(record) if isinstance(record, VectorRecord) else dict(record)])

    def delete(self, ids: list[str]) -> int:
        """Delete the ids that exist and return how many did."""
        existing = [str(item) for item in ids if str(item) and self.contains_id(str(item))]
        if existing:
            self.delete_by_ids(existing)
        return len(existing)

    def audit_counts(self) -> dict[str, int]:
        counts = Counter(self.list_ids())
        return {
            "physical_rows": counts.total(),
            "unique_ids": len(counts),
            "duplicate_rows": sum(count - 1 for count in counts.values() if count > 1),
            "duplicate_ids": sum(1 for count in counts.values() if count > 1),
        }


__all__ = ["VectorRecord", "VectorStore", "VectorStoreCompatibilityError"]
