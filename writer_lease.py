"""Cross-process truth-database writer lease.

Issue #39's corruption class is two independent processes each opening a
writable SQLite pager against the same truth database. SQLite WAL already
allows one writer plus many readers across processes; this module does not
change that. It makes *write ownership* explicit so cooperating provider
processes do not become a second uncoordinated writer.

Exactly one process per storage directory may hold the OS lease. Later
provider instances degrade to read-only recall. Same-process peer providers
share one refcounted OS lock so issue #43 dirty-peer recovery still works.
The lock is process-lifetime: the kernel releases it on crash or exit.
Shared registry state is bound to ``os.getpid()``. A fork child closes
inherited lock-handle copies without unlinking the parent sidecar and must
attempt a fresh OS lock instead of joining inherited same-process state.

If this host has no flock/msvcrt primitive, acquire fails closed.
Diagnostics stay sanitized: role only, never paths, usernames, hostnames,
secrets, or database content.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, IO, Iterator

try:  # pragma: no cover - exercised on POSIX hosts
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None
try:  # pragma: no cover - exercised on Windows hosts
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    _msvcrt = None

logger = logging.getLogger(__name__)

TRUTH_WRITER_LEASE_FILENAME = ".truth-writer.lease"
TRUTH_WRITER_LEASE_INFO_FILENAME = ".truth-writer.lease.info"

ALLOWED_TRUTH_WRITER_ROLES = frozenset(
    {
        "provider",
        "save_config",
        "journal_digest",
        "nightly_digest",
        "truth_connection",
    }
)
_UNKNOWN_TRUTH_WRITER_ROLE = "unknown"

# Win32 extended-length namespaces. Lengths are the marker prefixes, not path
# components: ``\\?\`` (4) and ``\\?\UNC\`` (8).
_WINDOWS_EXTENDED_PREFIX = "\\\\?\\"
_WINDOWS_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"

_SHARED_STATE_NAME = "_scope_recall_truth_writer_lease_state"


def _shared_lock_and_registry() -> tuple[threading.Lock, dict[str, _ProcessLeaseState]]:
    """Return the process-wide lease registry, even if this file is imported twice.

    Tests and production may load this module as ``writer_lease``,
    ``scope_recall.writer_lease``, and the Hermes plugin copy at once. The OS
    lock is per-process; the refcount must be too, or same-process join fails.
    The holder is published with ``setdefault`` only after lock and registry
    exist, so two first importers cannot split the process-wide state.
    Shared state is bound to ``os.getpid()`` so a fork child cannot join
    inherited same-process ownership.
    """

    candidate = types.ModuleType(_SHARED_STATE_NAME)
    setattr(candidate, "lock", threading.Lock())
    setattr(candidate, "registry", {})
    setattr(candidate, "pid", os.getpid())
    setattr(candidate, "poisoned", False)
    holder = sys.modules.setdefault(_SHARED_STATE_NAME, candidate)
    lock = getattr(holder, "lock", None)
    registry = getattr(holder, "registry", None)
    if not isinstance(lock, type(threading.Lock())):
        lock = threading.Lock()
        setattr(holder, "lock", lock)
    if not isinstance(registry, dict):
        registry = {}
        setattr(holder, "registry", registry)
    if getattr(holder, "pid", None) is None:
        setattr(holder, "pid", os.getpid())
    if not hasattr(holder, "poisoned"):
        setattr(holder, "poisoned", False)
    return lock, registry


def _bind_module_shared_state(holder: types.ModuleType) -> None:
    """Point this import alias at the central lock and registry."""

    global _PROCESS_REGISTRY_LOCK, _PROCESS_REGISTRY
    _PROCESS_REGISTRY_LOCK = holder.lock
    _PROCESS_REGISTRY = holder.registry


def _reset_inherited_writer_lease_state() -> None:
    """Replace inherited fork state. The first caller wins; later aliases rebind.

    Inherited lock-handle copies are closed without unlinking the parent
    sidecar. If an inherited close fails, the child is poisoned and must
    fail closed instead of joining or locking.
    """

    holder = sys.modules.get(_SHARED_STATE_NAME)
    current_pid = os.getpid()
    if holder is None:
        _shared_lock_and_registry()
        _bind_module_shared_state(sys.modules[_SHARED_STATE_NAME])
        return
    if getattr(holder, "pid", None) == current_pid:
        _bind_module_shared_state(holder)
        return
    old_registry = getattr(holder, "registry", {}) or {}
    poisoned = False
    if isinstance(old_registry, dict):
        for state in list(old_registry.values()):
            handle = getattr(state, "handle", None)
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                poisoned = True
    setattr(holder, "lock", threading.Lock())
    setattr(holder, "registry", {})
    setattr(holder, "pid", current_pid)
    setattr(holder, "poisoned", poisoned)
    _bind_module_shared_state(holder)


def _after_fork_in_child() -> None:
    """``os.register_at_fork`` child hook; safe if several aliases register it."""

    _reset_inherited_writer_lease_state()


def _refresh_shared_state() -> None:
    """Rebind alias globals and reset inherited state on PID mismatch."""

    holder = sys.modules.get(_SHARED_STATE_NAME)
    if holder is None:
        _shared_lock_and_registry()
        holder = sys.modules[_SHARED_STATE_NAME]
    holder_pid = getattr(holder, "pid", None)
    if holder_pid is None:
        setattr(holder, "pid", os.getpid())
        holder_pid = os.getpid()
    if holder_pid != os.getpid():
        _reset_inherited_writer_lease_state()
        return
    _bind_module_shared_state(holder)


def _shared_state_poisoned() -> bool:
    holder = sys.modules.get(_SHARED_STATE_NAME)
    return bool(holder is not None and getattr(holder, "poisoned", False))


def _register_fork_hook() -> None:
    register = getattr(os, "register_at_fork", None)
    if not callable(register):
        return
    try:
        register(after_in_child=_after_fork_in_child)
    except Exception:  # pragma: no cover - host may reject duplicate hooks
        pass


class _ProcessLeaseState:
    """One process-wide OS lock shared by every in-process lease holder.

    ``holders`` counts named owners (provider, digest, save_config).
    ``connection_pins`` is the connection-level refcount for live
    ``memory.sqlite3`` pagers so last-release of a named owner cannot drop
    OS authority while a helper-local connection still exists.
    """

    def __init__(self, handle: IO[bytes]) -> None:
        self.handle = handle
        self.holders = 0
        self.connection_pins = 0


_PROCESS_REGISTRY_LOCK, _PROCESS_REGISTRY = _shared_lock_and_registry()
_register_fork_hook()


class TruthWriterBusyError(RuntimeError):
    """Standalone writer could not obtain the truth-database writer lease.

    The message is sanitized: role/scope only. Paths, PIDs, hostnames, and
    database content must never appear here.
    """

    def __init__(
        self,
        *,
        role: str = "writer",
        scope: str = "",
        owner: dict[str, Any] | None = None,
    ) -> None:
        self.role = _canonical_lease_role(role)
        self.scope = str(scope or "")
        self.owner = sanitized_truth_writer_owner(owner)
        super().__init__(
            "truth_writer_busy: another Scope Recall process holds the "
            "truth-database writer lease"
        )


def _lease_paths(storage_dir: Path) -> tuple[Path, Path]:
    resolved = Path(storage_dir).expanduser().resolve(strict=False)
    return (
        resolved / TRUTH_WRITER_LEASE_FILENAME,
        resolved / TRUTH_WRITER_LEASE_INFO_FILENAME,
    )


def _strip_windows_extended_prefix(raw: str) -> str:
    """Map Win32 ``\\\\?\\`` / ``\\\\?\\UNC\\`` namespaces onto ordinary text.

    ``Path.resolve(strict=False)`` may return either form for the same
    directory across a create/resolve race. The process-wide lease registry
    must treat them as one key. POSIX paths and already-ordinary Win32 paths
    are unchanged. The UNC marker is matched case-insensitively so this stays
    correct after ``normcase`` and for mixed-case ``\\\\?\\Unc\\`` input.
    """

    text = str(raw)
    unc_len = len(_WINDOWS_EXTENDED_UNC_PREFIX)
    prefix_len = len(_WINDOWS_EXTENDED_PREFIX)
    if text[:unc_len].casefold() == _WINDOWS_EXTENDED_UNC_PREFIX.casefold():
        return "\\\\" + text[unc_len:]
    if text[:prefix_len].casefold() == _WINDOWS_EXTENDED_PREFIX.casefold():
        return text[prefix_len:]
    return text


def _canonical_registry_key(lease_path: Path) -> str:
    """Join Windows case/junction/extended-prefix aliases onto one owner."""

    resolved = Path(lease_path).expanduser().resolve(strict=False)
    folded = os.path.normcase(os.path.normpath(os.fspath(resolved)))
    return _strip_windows_extended_prefix(folded)


def _canonical_lease_role(role: str) -> str:
    """Return a fixed production role, or unknown for anything else."""

    clean_role = str(role or "").strip()
    if clean_role in ALLOWED_TRUTH_WRITER_ROLES:
        return clean_role
    return _UNKNOWN_TRUTH_WRITER_ROLE


def _sanitized_owner(role: str) -> dict[str, Any]:
    return {"role": _canonical_lease_role(role)}


def sanitized_truth_writer_owner(owner: Any) -> dict[str, Any]:
    """Return a role-only owner dict that never echoes sidecar or caller text."""

    if isinstance(owner, dict):
        return _sanitized_owner(str(owner.get("role") or ""))
    return _sanitized_owner(str(owner or ""))


def read_truth_writer_owner(storage_dir: Path) -> dict[str, Any]:
    """Best-effort diagnostic read of the sanitized writer-owner sidecar."""

    _, info_path = _lease_paths(Path(storage_dir))
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return sanitized_truth_writer_owner(payload)


def _os_lock_available() -> bool:
    posix_flock = getattr(_fcntl, "flock", None) if _fcntl is not None else None
    windows_locking = getattr(_msvcrt, "locking", None) if _msvcrt is not None else None
    return callable(posix_flock) or callable(windows_locking)


def _try_lock_exclusive_nonblocking(handle: IO[bytes]) -> bool:
    """One non-blocking exclusive lock attempt on byte 0 of the lease file."""

    posix_flock = getattr(_fcntl, "flock", None) if _fcntl is not None else None
    if callable(posix_flock):
        try:
            posix_flock(
                handle.fileno(),
                int(getattr(_fcntl, "LOCK_EX")) | int(getattr(_fcntl, "LOCK_NB")),
            )
            return True
        except OSError:
            return False
    windows_locking = (
        getattr(_msvcrt, "locking", None) if _msvcrt is not None else None
    )
    if callable(windows_locking):
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            windows_locking(handle.fileno(), int(getattr(_msvcrt, "LK_NBLCK")), 1)
            return True
        except OSError:
            return False
    return False


class TruthWriterLease:
    """Per-process writer lease handle for one storage directory.

    Cross-process exclusive, in-process shared (refcounted). The OS lock dies
    with the process, so a crashed writer never strands the lease.
    """

    def __init__(self, storage_dir: Path, *, role: str = "provider") -> None:
        self._storage_dir = Path(storage_dir).expanduser().resolve(strict=False)
        self._role = _canonical_lease_role(role)
        self._lease_path, self._info_path = _lease_paths(self._storage_dir)
        self._registry_key = _canonical_registry_key(self._lease_path)
        self._acquired = False
        self._acquired_pid: int | None = None
        self._pin_only = False

    @property
    def acquired(self) -> bool:
        return bool(self._acquired and self._acquired_pid == os.getpid())

    def acquire(self) -> dict[str, Any]:
        """Acquire or join this process's writer lease, or report busy.

        Returns ``{"status": "acquired", ...}`` or
        ``{"status": "busy", "scope": ..., "owner": {...}}``. A new registry
        entry created by ``truth_connection`` is pin-only; a named provider
        creates one holder. Existing-state joins keep that role split. A
        second in-process holder joins the refcounted OS lock. A different
        process holding the lease, a missing OS primitive, or a lease-file
        error produces ``busy`` and the caller must fail closed for writes.
        """

        _refresh_shared_state()
        if self._acquired and self._acquired_pid == os.getpid():
            return {"status": "acquired", "owner": _sanitized_owner(self._role)}
        if self._acquired:
            self._acquired = False
            self._acquired_pid = None
        if _shared_state_poisoned():
            return {"status": "busy", "scope": "fork_state_error", "owner": {}}
        if not _os_lock_available():
            return {
                "status": "busy",
                "scope": "unsupported_platform",
                "owner": {},
            }
        holder = sys.modules.get(_SHARED_STATE_NAME)
        registry_lock = getattr(holder, "lock", _PROCESS_REGISTRY_LOCK)
        registry = getattr(holder, "registry", _PROCESS_REGISTRY)
        # Lookup, the nonblocking OS lock, and registration are one critical
        # section so two in-process threads cannot both miss the registry and
        # treat the loser as a false cross-process busy. Recompute the registry
        # key after mkdir so a create/resolve race cannot split one directory
        # onto two keys.
        with registry_lock:
            try:
                self._lease_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("Scope Recall writer lease file unavailable")
                return {"status": "busy", "scope": "lease_file_error", "owner": {}}
            key = _canonical_registry_key(self._lease_path)
            self._registry_key = key
            state = registry.get(key)
            if state is not None:
                if self._role == "truth_connection":
                    state.connection_pins += 1
                    self._pin_only = True
                else:
                    state.holders += 1
                    self._pin_only = False
                self._acquired = True
                self._acquired_pid = os.getpid()
                return {
                    "status": "acquired",
                    "scope": "same_process_shared",
                    "owner": _sanitized_owner(self._role),
                }
            try:
                handle = self._lease_path.open("a+b")
            except OSError:
                logger.warning("Scope Recall writer lease file unavailable")
                return {"status": "busy", "scope": "lease_file_error", "owner": {}}
            if _try_lock_exclusive_nonblocking(handle):
                state = _ProcessLeaseState(handle)
                if self._role == "truth_connection":
                    # Connection-first authority is a pin, not a named holder.
                    # A later provider join must see holders=0/pins=1 here.
                    state.holders = 0
                    state.connection_pins = 1
                    self._pin_only = True
                else:
                    state.holders = 1
                    state.connection_pins = 0
                    self._pin_only = False
                registry[key] = state
                self._acquired = True
                self._acquired_pid = os.getpid()
                self._write_owner_info()
                return {"status": "acquired", "owner": _sanitized_owner(self._role)}
            try:
                handle.close()
            except OSError:
                pass
            return {
                "status": "busy",
                "scope": "cross_process",
                "owner": read_truth_writer_owner(self._storage_dir),
            }

    def release(self) -> None:
        """Drop one holder. Last release closes the locked OS handle under the lock.

        Non-last holders only decrement the refcount. The last holder keeps
        ``holders==1``, the registry entry, and ``self._acquired`` until the
        single OS authority release — closing the locked file handle — succeeds.
        POSIX flock and Windows msvcrt locks are released by that close; there
        is no separate unlock step that could drop authority before close.

        Sidecar removal and handle close stay in one critical section so a
        same-process acquire cannot observe registry-absent + OS-still-locked,
        and an old releaser cannot unlink a newly acquired owner's sidecar.
        Missing registry or an invalid holder count fail observably without
        pretending the lease was released.
        """

        _refresh_shared_state()
        key = self._registry_key
        holder = sys.modules.get(_SHARED_STATE_NAME)
        registry_lock = getattr(holder, "lock", _PROCESS_REGISTRY_LOCK)
        registry = getattr(holder, "registry", _PROCESS_REGISTRY)
        with registry_lock:
            if not self._acquired or self._acquired_pid != os.getpid():
                return
            state = registry.get(key)
            if state is None:
                raise RuntimeError(
                    "truth writer lease registry entry missing during release"
                )
            if self._pin_only:
                if state.connection_pins < 1:
                    raise RuntimeError(
                        "truth writer connection pin count is invalid during release"
                    )
                if state.holders > 0 or state.connection_pins > 1:
                    state.connection_pins -= 1
                    self._acquired = False
                    self._acquired_pid = None
                    self._pin_only = False
                    return
                # This is the last authority reference (holders=0, pins=1).
                # Keep both the registry count and this lease acquired until
                # the locked handle is confirmed closed, exactly as for the
                # last named holder below. A failed close must be retryable by
                # the connection that still owns this pin.
            elif state.holders < 1:
                raise RuntimeError(
                    "truth writer lease holder count is invalid during release"
                )
            elif state.holders > 1:
                state.holders -= 1
                self._acquired = False
                self._acquired_pid = None
                return
            elif state.connection_pins > 0:
                state.holders -= 1
                self._acquired = False
                self._acquired_pid = None
                return
            try:
                self._info_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - stale info is diagnostic only
                pass
            handle = state.handle
            close_error: Exception | None = None
            try:
                handle.close()
            except Exception as exc:
                close_error = exc
            closed = bool(getattr(handle, "closed", False))
            if close_error is not None and not closed:
                raise close_error
            if not closed:
                raise RuntimeError(
                    "truth writer lease handle close did not release OS authority"
                )
            del registry[key]
            self._acquired = False
            self._acquired_pid = None
            self._pin_only = False
            if close_error is not None:
                raise close_error

    def _write_owner_info(self) -> None:
        try:
            self._info_path.write_text(
                json.dumps(_sanitized_owner(self._role), ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:  # pragma: no cover - sidecar is diagnostic only
            logger.debug("Scope Recall writer lease info sidecar write failed")


@contextmanager
def holding_truth_writer_lease(
    storage_dir: Path, *, role: str = "provider"
) -> Iterator[TruthWriterLease]:
    """Acquire the canonical writer lease or fail closed; always release."""

    lease = TruthWriterLease(storage_dir, role=role)
    result = lease.acquire()
    if result.get("status") != "acquired":
        owner = result.get("owner")
        raise TruthWriterBusyError(
            role=role,
            scope=str(result.get("scope") or ""),
            owner=owner if isinstance(owner, dict) else {},
        )
    try:
        yield lease
    finally:
        lease.release()


__all__ = [
    "ALLOWED_TRUTH_WRITER_ROLES",
    "TRUTH_WRITER_LEASE_FILENAME",
    "TRUTH_WRITER_LEASE_INFO_FILENAME",
    "TruthWriterBusyError",
    "TruthWriterLease",
    "holding_truth_writer_lease",
    "read_truth_writer_owner",
    "sanitized_truth_writer_owner",
]
