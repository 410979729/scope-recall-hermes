"""Cross-thread/process guard for physical vector companion mutations.

SQLite truth transactions must never span embedding or vector-backend I/O.  This
module provides the separate physical-mutation serialization boundary used by
ordinary outbox replay and backend-specific writers.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Any, Iterator

try:  # pragma: no cover - exercised on POSIX release hosts
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None
try:  # pragma: no cover - exercised on Windows release hosts
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    _msvcrt = None

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_THREAD_STATE = threading.local()


@contextmanager
def _held(lock: Any) -> Iterator[None]:
    """Hold an optional caller lock before acquiring the process-wide file lock."""

    if lock is None:
        yield
        return
    with lock:
        yield


@contextmanager
def advisory_file_lock(lock_path: Path) -> Iterator[None]:
    """Serialize one physical resource across threads and independent processes.

    A per-path thread lock is required because advisory file locks do not have
    portable same-process thread semantics.  POSIX uses ``flock`` and Windows
    uses a one-byte ``msvcrt.locking`` region; unsupported platforms still retain
    deterministic in-process serialization.
    """

    resolved_path = Path(lock_path).expanduser().resolve(strict=False)
    key = str(resolved_path)
    with _PATH_LOCKS_GUARD:
        thread_lock = _PATH_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        depths = getattr(_THREAD_STATE, "depths", None)
        if depths is None:
            depths = {}
            _THREAD_STATE.depths = depths
        current_depth = int(depths.get(key, 0))
        if current_depth:
            depths[key] = current_depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        with resolved_path.open("a+b") as handle:
            windows_locking = (
                getattr(_msvcrt, "locking", None) if _msvcrt is not None else None
            )
            posix_locking = (
                getattr(_fcntl, "flock", None) if _fcntl is not None else None
            )
            posix_lock_ex = (
                getattr(_fcntl, "LOCK_EX", None) if _fcntl is not None else None
            )
            posix_lock_un = (
                getattr(_fcntl, "LOCK_UN", None) if _fcntl is not None else None
            )
            using_posix_lock = False
            if callable(posix_locking) and posix_lock_ex is not None:
                posix_locking(handle.fileno(), int(posix_lock_ex))
                using_posix_lock = True
            elif callable(windows_locking):
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                windows_locking(
                    handle.fileno(), int(getattr(_msvcrt, "LK_LOCK")), 1
                )
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                if (
                    using_posix_lock
                    and callable(posix_locking)
                    and posix_lock_un is not None
                ):
                    posix_locking(handle.fileno(), int(posix_lock_un))
                elif callable(windows_locking):
                    handle.seek(0)
                    windows_locking(
                        handle.fileno(), int(getattr(_msvcrt, "LK_UNLCK")), 1
                    )


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
