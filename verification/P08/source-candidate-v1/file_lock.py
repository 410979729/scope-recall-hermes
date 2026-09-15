"""Portable advisory file locking shared by persistent plugin resources.

Callers provide the resource-specific lock filename; this module owns only the
cross-thread/process serialization primitive and has no dependency on storage,
identity, or vector-domain code.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Iterator

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
def advisory_file_lock(lock_path: Path) -> Iterator[None]:
    """Serialize one physical resource across threads and processes.

    A per-path thread lock is required because advisory file locks do not have
    portable same-process thread semantics. POSIX uses ``flock`` and Windows
    uses a one-byte ``msvcrt.locking`` region; unsupported platforms still keep
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


__all__ = ["advisory_file_lock"]
