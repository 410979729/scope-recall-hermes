"""Portable advisory file locking shared by persistent plugin resources."""
from __future__ import annotations
from contextlib import contextmanager
import errno
import math
from pathlib import Path
import threading
import time
from typing import Iterator
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_THREAD_STATE = threading.local()
def _busy_lock_error(exc: BaseException) -> bool:
    code = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    return code in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK} or winerror in {33, 36}
@contextmanager
def advisory_file_lock(lock_path: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    """Serialize one physical resource with an optional cumulative deadline."""
    if timeout_seconds is not None and (type(timeout_seconds) not in (int, float) or not math.isfinite(float(timeout_seconds)) or timeout_seconds < 0):
        raise ValueError("timeout_seconds must be non-negative and finite")
    deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
    resolved_path = Path(lock_path).expanduser().resolve(strict=False)
    key = str(resolved_path)
    with _PATH_LOCKS_GUARD:
        thread_lock = _PATH_LOCKS.setdefault(key, threading.RLock())
    acquired = thread_lock.acquire() if deadline is None else thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
    if not acquired:
        raise TimeoutError("advisory file lock timeout")
    try:
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
            windows_locking = getattr(_msvcrt, "locking", None) if _msvcrt is not None else None
            fcntl_module = _fcntl
            posix_locking = getattr(fcntl_module, "flock", None) if fcntl_module is not None else None
            lock_ex = getattr(fcntl_module, "LOCK_EX", None) if fcntl_module is not None else None
            lock_un = getattr(fcntl_module, "LOCK_UN", None) if fcntl_module is not None else None
            lock_nb = int(getattr(fcntl_module, "LOCK_NB", 0)) if fcntl_module is not None else 0
            using_posix_lock = False
            if callable(posix_locking) and lock_ex is not None:
                while True:
                    try:
                        flags = int(lock_ex) | (lock_nb if deadline is not None else 0)
                        posix_locking(handle.fileno(), flags)
                        using_posix_lock = True
                        break
                    except OSError as exc:
                        if deadline is None or not _busy_lock_error(exc):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("advisory file lock timeout") from exc
                        time.sleep(min(0.01, remaining))
            elif callable(windows_locking):
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                while True:
                    try:
                        mode = int(getattr(_msvcrt, "LK_LOCK")) if deadline is None else int(getattr(_msvcrt, "LK_NBLCK"))
                        windows_locking(handle.fileno(), mode, 1)
                        break
                    except OSError as exc:
                        if deadline is None or not _busy_lock_error(exc):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("advisory file lock timeout") from exc
                        time.sleep(min(0.01, remaining))
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                if using_posix_lock and lock_un is not None and callable(posix_locking):
                    posix_locking(handle.fileno(), int(lock_un))
                elif callable(windows_locking):
                    handle.seek(0)
                    windows_locking(handle.fileno(), int(getattr(_msvcrt, "LK_UNLCK")), 1)
    finally:
        thread_lock.release()
__all__ = ["advisory_file_lock"]
