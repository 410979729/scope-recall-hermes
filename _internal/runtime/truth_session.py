"""Single owner of one published SQLite connection lifetime.

TruthSession owns require / commit / rollback / recover / close. Join of an
already-open outer transaction is explicit and nestable via
``joining_outer()``. ``require()`` never commits and never changes join
state. ``commit()`` is a no-op while any join scope is active. Durable write
commits stay with write_kernel / transaction_guard.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)


def _as_sqlite_connection(value: Any) -> sqlite3.Connection:
    if isinstance(value, sqlite3.Connection):
        return value
    raise RuntimeError("Scope Recall open_writer did not return a SQLite connection")


class TruthSession:
    """Published-connection handle for one provider adapter."""

    def __init__(self, owner: Any | None = None) -> None:
        self._owner = owner
        self._conn: sqlite3.Connection | None = None
        self._join_depth = 0
        self._own_lock = threading.RLock()

    @property
    def db_path(self) -> Any:
        owner = self._owner
        return None if owner is None else getattr(owner, "_db_path", None)

    @property
    def _joined_outer(self) -> bool:
        return self._join_depth > 0

    def lock(self) -> threading.RLock:
        owner = self._owner
        lock = None if owner is None else getattr(owner, "_lock", None)
        if lock is None:
            return self._own_lock
        return lock

    @contextmanager
    def joining_outer(self) -> Iterator["TruthSession"]:
        """Nestable scope that joins a caller-owned outer transaction.

        Depth is restored on nested exit and on exception. ``commit()`` is a
        no-op while the depth is positive. After the outermost exit, a later
        session-owned transaction may commit.
        """

        self._join_depth += 1
        try:
            yield self
        finally:
            self._join_depth -= 1

    def _owner_flag(self, name: str) -> bool:
        owner = self._owner
        if owner is None:
            return False
        getter = getattr(owner, name, None)
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except AttributeError:
            return False

    def require(self) -> sqlite3.Connection:
        """Return the published connection, opening one if needed.

        This method never commits and never changes join state.
        """

        owner = self._owner
        if self._owner_flag("_runtime_memory_disabled"):
            from ...scope import RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL

            raise RuntimeError(RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL)
        conn = self._conn
        if conn is not None:
            return conn
        if owner is None:
            raise RuntimeError("Scope Recall database path is not initialized")
        shutdown = getattr(owner, "_shutdown_requested", None)
        is_set = getattr(shutdown, "is_set", None)
        if callable(is_set) and is_set():
            raise RuntimeError("Scope Recall is shutting down")
        if self._owner_flag("_truth_writes_blocked"):
            raise RuntimeError("truth_writer_busy")
        with self.lock():
            if self._conn is None:
                if self._owner_flag("_truth_writes_blocked"):
                    raise RuntimeError("truth_writer_busy")
                opener = getattr(owner, "_open_runtime_connection", None)
                if not callable(opener):
                    raise RuntimeError("Scope Recall database path is not initialized")
                self._conn = _as_sqlite_connection(opener())
            assert self._conn is not None
            return self._conn

    def commit(self) -> None:
        """Commit only a session-owned transaction.

        While ``joining_outer()`` is active this is a no-op so the caller
        remains the durable commit owner.
        """

        if self._join_depth > 0:
            return
        conn = self._conn
        if conn is None:
            return
        try:
            in_txn = bool(conn.in_transaction)
        except sqlite3.ProgrammingError:
            return
        if not in_txn:
            return
        conn.commit()

    def close_published(
        self,
        conn: Any,
        *,
        context: str,
        reraise: bool = True,
    ) -> bool:
        try:
            conn.close()
        except Exception:
            owner = self._owner
            if owner is not None and bool(
                getattr(owner, "_writer_handoff_fenced", False)
            ):
                logger.warning(
                    "Scope Recall SQLite close failed during fenced writer handoff"
                )
            else:
                logger.exception("Scope Recall SQLite close failed after %s", context)
            if self._conn is conn:
                if owner is not None:
                    owner._truth_writer_role = "unknown"
            if reraise:
                raise
            return False
        if self._conn is conn:
            self._conn = None
        return True

    def quarantine(self, conn: Any, context: str) -> None:
        """Detach and close a connection whose transactional state is untrusted."""

        self.close_published(
            conn,
            context=f"quarantining after {context}",
            reraise=False,
        )

    def rollback_after_error(self, context: str) -> None:
        owner = self._owner
        lock = getattr(owner, "_lock", None) if owner is not None else None
        if lock is None:
            lock = self._own_lock
        with lock:
            self._rollback_unlocked(context)

    def _rollback_unlocked(self, context: str) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            in_transaction = bool(conn.in_transaction)
        except sqlite3.ProgrammingError:
            self.close_published(
                conn,
                context=f"checking transaction state after {context}",
                reraise=False,
            )
            return
        if not in_transaction:
            return
        try:
            conn.rollback()
        except Exception:
            logger.exception("Scope Recall SQLite rollback failed after %s", context)
            self.quarantine(conn, context)

    @staticmethod
    def _merge_peer_recovery_report(
        payload: dict[str, Any], report: dict[str, Any]
    ) -> None:
        """Merge one nonblocking peer sweep without double-counting peers."""

        counters = {
            "peer_providers_checked",
            "peer_rollbacks",
            "peer_rollback_errors",
            "peer_busy_skipped",
        }
        for key, value in report.items():
            if key not in counters:
                payload[key] = value
        payload["peer_providers_checked"] = max(
            int(payload.get("peer_providers_checked", 0) or 0),
            int(report.get("peer_providers_checked", 0) or 0),
        )
        payload["peer_rollbacks"] = int(payload.get("peer_rollbacks", 0) or 0) + int(
            report.get("peer_rollbacks", 0) or 0
        )
        payload["peer_rollback_errors"] = int(
            payload.get("peer_rollback_errors", 0) or 0
        ) + int(report.get("peer_rollback_errors", 0) or 0)
        latest_busy = int(report.get("peer_busy_skipped", 0) or 0)
        payload["peer_busy_skipped"] = latest_busy
        payload["peer_busy_skipped_total"] = int(
            payload.get("peer_busy_skipped_total", 0) or 0
        ) + latest_busy
        payload["peer_recovery_passes"] = int(
            payload.get("peer_recovery_passes", 0) or 0
        ) + 1

    def recover_after_error(
        self,
        context: str,
        *,
        peer_rollback: Callable[[str], dict[str, Any]] | None = None,
        open_writer: Callable[[], sqlite3.Connection] | None = None,
        write_probe: Callable[[sqlite3.Connection], bool] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "recovered": False,
            "rolled_back": False,
            "reopened": False,
            "write_probe": False,
            "reconnect_pending": False,
        }
        first_peer_busy = 0
        first_peer_errors = 0
        if peer_rollback is not None:
            first_peer_report = dict(peer_rollback(context))
            self._merge_peer_recovery_report(payload, first_peer_report)
            first_peer_busy = int(first_peer_report.get("peer_busy_skipped", 0) or 0)
            first_peer_errors = int(
                first_peer_report.get("peer_rollback_errors", 0) or 0
            )
        owner = self._owner
        lock = getattr(owner, "_lock", None) if owner is not None else None
        if lock is None:
            lock = self._own_lock
        first_peer_unresolved = bool(first_peer_busy or first_peer_errors)
        with lock:
            payload = self._recover_unlocked(
                context,
                payload,
                open_writer=open_writer,
                write_probe=write_probe,
                defer_reopen_on_probe_failure=first_peer_unresolved,
            )
            should_recheck_peer = (
                peer_rollback is not None
                and first_peer_busy > 0
                and first_peer_errors == 0
                and not bool(payload.get("recovered"))
                and not bool(payload.get("reconnect_pending"))
                and self._conn is not None
            )
        if not should_recheck_peer:
            if first_peer_unresolved:
                payload["peer_recovery_deferred"] = not bool(payload.get("recovered"))
            return payload

        # A peer may have held its lifecycle only for a short maintenance unit.
        # Recheck once after the first write probe, outside our connection lock.
        # Both sweeps remain nonblocking and the provider lifecycle stays held by
        # the caller, preserving lifecycle -> provider-lock ordering.
        assert peer_rollback is not None
        second_peer_report = dict(peer_rollback(context))
        self._merge_peer_recovery_report(payload, second_peer_report)
        unresolved_peer = bool(
            int(second_peer_report.get("peer_busy_skipped", 0) or 0)
            or int(second_peer_report.get("peer_rollback_errors", 0) or 0)
        )
        with lock:
            payload = self._recover_unlocked(
                context,
                payload,
                open_writer=open_writer,
                write_probe=write_probe,
                defer_reopen_on_probe_failure=unresolved_peer,
            )
        payload["peer_recovery_deferred"] = unresolved_peer and not bool(
            payload.get("recovered")
        )
        return payload

    def _recover_unlocked(
        self,
        context: str,
        payload: dict[str, Any],
        *,
        open_writer: Callable[[], sqlite3.Connection] | None,
        write_probe: Callable[[sqlite3.Connection], bool] | None,
        defer_reopen_on_probe_failure: bool = False,
    ) -> dict[str, Any]:
        conn = self._conn
        if conn is None:
            return payload
        rollback_failed = False
        if conn.in_transaction:
            try:
                conn.rollback()
                payload["rolled_back"] = True
            except Exception:
                logger.exception(
                    "Scope Recall SQLite rollback failed during recovery after %s",
                    context,
                )
                self.quarantine(conn, context)
                rollback_failed = True
        probe = write_probe if write_probe is not None else self.probe_write
        if not rollback_failed and probe(conn):
            payload["recovered"] = True
            payload["write_probe"] = True
            return payload
        if not rollback_failed and defer_reopen_on_probe_failure:
            return payload
        owner = self._owner
        db_path = None if owner is None else getattr(owner, "_db_path", None)
        if db_path is None:
            return payload
        if rollback_failed:
            if self._conn is not None:
                payload["reconnect_pending"] = True
                return payload
        elif not self.close_published(
            conn,
            context=f"recovery after {context}",
            reraise=False,
        ):
            payload["reconnect_pending"] = True
            return payload
        opener = open_writer
        if opener is None and owner is not None:
            maybe = getattr(owner, "_open_runtime_connection", None)
            opener = maybe if callable(maybe) else None
        if opener is None:
            payload["reconnect_pending"] = True
            return payload
        try:
            reopened = _as_sqlite_connection(opener())
            payload["reopened"] = True
            payload["write_probe"] = probe(reopened)
            payload["recovered"] = bool(payload["write_probe"])
            payload["reconnect_pending"] = not payload["recovered"]
            if not payload["recovered"]:
                self.close_published(
                    reopened,
                    context=f"failed write probe after recovery {context}",
                    reraise=False,
                )
        except Exception:
            if self._conn is not None and owner is not None:
                owner._truth_writer_role = "unknown"
            payload["reconnect_pending"] = True
            logger.exception(
                "Scope Recall SQLite reopen failed during recovery after %s",
                context,
            )
        return payload

    def probe_write(self, conn: sqlite3.Connection) -> bool:
        from .storage import probe_sqlite_write

        return probe_sqlite_write(conn)

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            conn.close()
