"""Cross-thread/process guard for physical vector companion mutations.

SQLite truth transactions must never span embedding or vector-backend I/O.  This
module provides the separate physical-mutation serialization boundary used by
ordinary outbox replay and backend-specific writers.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .file_lock import advisory_file_lock


@contextmanager
def _held(lock: Any) -> Iterator[None]:
    """Hold an optional caller lock before acquiring the process-wide file lock."""

    if lock is None:
        yield
        return
    with lock:
        yield


@contextmanager
def vector_mutation_guard(
    *,
    thread_lock: Any = None,
    storage_dir: Path | str | None = None,
) -> Iterator[None]:
    """Hold the provider lock and, when available, its cross-process write lock."""

    with _held(thread_lock):
        if storage_dir is None or not str(storage_dir).strip():
            yield
            return
        with advisory_file_lock(Path(storage_dir) / ".vector-mutation.lock"):
            yield


__all__ = ["advisory_file_lock", "vector_mutation_guard"]
