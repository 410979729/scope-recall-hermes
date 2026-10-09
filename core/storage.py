"""The sole core SQLite transaction boundary. No host or Provider dependencies."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..contracts import (
    ContractError,
    InstanceBinding,
    TrustedContext,
)
from .delete_storage import Deletions
from .failure_retry import failure_kind, retry_class
from .registry_storage import Registry
from .schema import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    STATEMENTS,
    UPGRADE_CHAIN,
    stale_header_schema,
    upgrade_1105,
    upgrade_1106,
    upgrade_1107,
    upgrade_1108,
    upgrade_1109,
)
from .source_records import SOURCE_COLUMNS, StoredSource, source_size, stored_source
from .source_storage import Sources
from .truth_connection import TruthDatabaseMode, connect_truth_database
from .visibility import allowed, allowed_refs
from .work_storage import WorkItems
from .writer_lease import TruthWriterBusyError

#: How often a writer looks again for another process's lease while it waits.
_LEASE_POLL_SECONDS = 0.01


def _directory(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _most_frequent(counts: dict[str, int]) -> tuple[tuple[str, int], ...]:
    """The sixteen largest counts, largest first."""
    return tuple(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:16])


def _cleanup_error(original: BaseException, cleanup: BaseException, stage: str) -> None:
    original.add_note(f"SQLite {stage} cleanup failed: {type(cleanup).__name__}; connection discarded")
    errors = getattr(original, "cleanup_errors", ())
    setattr(original, "cleanup_errors", (*errors, cleanup))
    if original.__cause__ is None:
        original.__cause__ = cleanup


@dataclass(frozen=True)
class StoreStatus:
    schema_version: int
    memory_epoch: int
    config_version: int
    sources: int
    pending_work: int
    failed_work: int = 0
    leased_work: int = 0
    oldest_pending_at: str | None = None
    #: Why the failed rows failed, one count per failure kind (``failure_retry.failure_kind``).
    work_error_counts: tuple[tuple[str, int], ...] = ()
    source_only_sources: int | None = None
    deferred_sources: int | None = None
    oldest_deferred_at: str | None = None
    #: The failed rows ``failure_retry.retry_class`` calls terminal: by design, cleared by no retry.
    terminal_failed_work: int = 0
    #: The error a queued row kept from its last attempt.  It is waiting, not failed, so it is told apart from the
    #: failures and counted in none of them.
    pending_error_counts: tuple[tuple[str, int], ...] = ()


#: Source versions ``Transaction.prefetch_sources`` loads per statement.
_PREFETCH_PAGE = 400
#: What one read transaction keeps (``Transaction.remember``), its text counted in characters, which Python holds in
#: a little over twice the room.  Over 483 recalls on a copy of the shared store: median 24 answers and 22,000 characters,
#: the largest 7,595 and 13 million.
_MEMO_ENTRIES = 16384
_MEMO_BYTES = 32 << 20


class Transaction:
    """Scoped repository operations; no public connection or SQL execution surface."""

    def __init__(self, connection: sqlite3.Connection, context: TrustedContext, *, writable: bool) -> None:
        self.__connection = connection
        self.context = context
        self.__writable = writable
        self.__active = True
        self.__poisoned = False
        self.__savepoint_sequence = 0
        self.__entry_labels: dict[str, dict[str, str]] | None = None
        #: What a read transaction loaded (``remembered``), and the size of the text it keeps; ``None`` in a write
        #: transaction.
        self.__memo: dict | None = None if writable else {}
        self.__memo_bytes = 0

    def entry_label(self, entry_id: str) -> dict[str, str] | None:
        """The ``{id, name}`` a reader is shown for a source's entry, or None.

        None in a local store, whose rows all say ``local`` and whose recall
        output is exactly what it was.  Read once per transaction.
        """
        if self.context.binding.installation_kind != "shared":
            return None
        if self.__entry_labels is None:
            self.__entry_labels = {
                key: {"id": key, "name": value["name"]} for key, value in self.registry.entries().items()
            }
        label = self.__entry_labels.get(entry_id)
        return dict(label) if label is not None else None

    def _check(self, *, write: bool = False) -> sqlite3.Connection:
        if not self.__active or self.__poisoned:
            raise ContractError("STORAGE_UNAVAILABLE", "transaction_closed")
        if write and not self.__writable:
            raise ContractError("ACCESS_DENIED", "read_only")
        return self.__connection

    @property
    def deletions(self):
        return Deletions(self)

    @property
    def episodes(self):
        from .episode_storage import Episodes  # on first use: most of a hook's processes never need it

        return Episodes(self)

    @property
    def artifacts(self):
        from .artifact_storage import Artifacts  # on first use: most of a hook's processes never need it

        return Artifacts(self)

    @property
    def sources(self):
        return Sources(self)

    @property
    def registry(self):
        return Registry(self)

    def _entries_changed(self) -> None:
        """An entry was registered or renamed in this transaction: ``entry_label`` reads them again."""
        self.__entry_labels = None

    @property
    def references(self):
        from .reference_storage import References  # on first use: most of a hook's processes never need it

        return References(self)

    def _finish(self) -> None:
        self.__active = False
        # What it loaded goes with it, also when a traceback keeps the transaction.
        if self.__memo is not None:
            self.__memo = {}
            self.__memo_bytes = 0

    def _assert_committable(self) -> None:
        self._check(write=True)

    @property
    def claims(self):
        from .claim_storage import Claims  # on first use: most of a hook's processes never need it

        self._check()
        return Claims(self)

    @property
    def work(self):
        self._check()
        return WorkItems(self)

    @property
    def candidates(self):
        from .candidate_storage import CandidateLifecycle  # on first use: most of a hook's processes never need it

        self._check()
        return CandidateLifecycle(self)

    @contextmanager
    def savepoint(self) -> Iterator[Transaction]:
        """Borrow the owning transaction; only this boundary manages savepoint SQL."""
        conn = self._check(write=True)
        self.__savepoint_sequence += 1
        name = f"core_{self.__savepoint_sequence}"
        active = False
        try:
            conn.execute(f"SAVEPOINT {name}")
            active = True
            yield self
            conn.execute(f"RELEASE SAVEPOINT {name}")
            active = False
        except BaseException as original:
            if active and conn.in_transaction:
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    conn.execute(f"RELEASE SAVEPOINT {name}")
                    active = False
                except BaseException as cleanup:
                    self.__poisoned = True
                    _cleanup_error(original, cleanup, "savepoint")
            raise

    def _scope(self, scope_id: str) -> None:
        if scope_id not in self.context.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED")

    def memory_epoch(self) -> int:
        """Read the authority fence in this transaction without queue diagnostics."""
        return int(self._check().execute("SELECT memory_epoch FROM instance_meta WHERE singleton=1").fetchone()[0])

    def status(
        self, *, include_all_projects: bool = False, include_admission: bool = False, include_queue_age: bool = True
    ) -> StoreStatus:
        conn = self._check()
        meta = conn.execute(
            "SELECT schema_version,memory_epoch,config_version FROM instance_meta WHERE singleton=1"
        ).fetchone()
        scopes = sorted(self.context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        context_filter = "AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)"
        params = (*scopes, self.context.project_id, self.context.branch_id)
        if include_all_projects:
            # Metadata-only installation diagnostics; scope isolation remains.
            context_filter, params = "", tuple(scopes)
        # The store's own size and the age of the oldest queued item are what an
        # operator reads; a pass reports its queue depth and its own items.  Both
        # walk every row, so a pass that asked for them paid for them once per
        # pass and more the fuller the queue was -- the wrong way round for a
        # report whose job is to say the queue is deep.
        source_count = 0
        if include_queue_age:
            source_count = conn.execute(
                f"SELECT count(*) FROM source_events WHERE read_blocked=0 AND scope_id IN ({marks}) {context_filter}",
                params,
            ).fetchone()[0]
        work_count = conn.execute(
            f"SELECT count(*) FROM work_items WHERE state IN ('pending','leased') AND scope_id IN ({marks}) {context_filter}",
            params,
        ).fetchone()[0]
        failed_count = conn.execute(
            f"SELECT count(*) FROM work_items WHERE state='failed' AND scope_id IN ({marks}) {context_filter}", params
        ).fetchone()[0]
        leased_count = conn.execute(
            f"SELECT count(*) FROM work_items WHERE state='leased' AND scope_id IN ({marks}) {context_filter}", params
        ).fetchone()[0]
        oldest = None
        if include_queue_age:
            oldest = conn.execute(
                f"""SELECT MIN(COALESCE((SELECT e.persisted_at FROM source_events e
                WHERE e.event_id=work_items.subject_ref AND e.source_revision=work_items.subject_revision),available_at))
                FROM work_items WHERE state IN ('pending','leased') AND scope_id IN ({marks}) {context_filter}""",
                params,
            ).fetchone()[0]
        # Detailed source processing counts are diagnostic-only; avoid a JSON
        # scan of all sources on every internal epoch/queue status read.
        admission = (None, None, None)
        if include_admission:
            admission = conn.execute(
                f"""SELECT
            COALESCE(SUM(json_extract(extra_json,'$._scope_recall_admission.disposition')='source_only'),0),
            COALESCE(SUM(json_extract(extra_json,'$._scope_recall_admission.disposition')='deferred'),0),
            MIN(CASE WHEN json_extract(extra_json,'$._scope_recall_admission.disposition')='deferred' THEN persisted_at END)
            FROM source_events WHERE read_blocked=0 AND suppressed=0 AND scope_id IN ({marks}) {context_filter}
            AND NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=source_events.source_group_key
                AND newer.source_revision>source_events.source_revision)
            AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                AND b.object_ref=source_events.event_id AND (b.read_blocked=1 OR b.suppressed=1))""",
                params,
            ).fetchone()
        errors = conn.execute(
            f"""SELECT state,last_error_code,COUNT(*) AS n FROM work_items
            WHERE state IN ('pending','failed') AND last_error_code IS NOT NULL
            AND scope_id IN ({marks}) {context_filter}
            GROUP BY state,last_error_code""",
            params,
        ).fetchall()
        # A code carries its retry history (``auto_retry:1|derivation_invalid``) and its writer's case; each kind is
        # counted once, as ``failure_retry.failure_kind`` reads it, and a failure is terminal as ``retry_class`` and so
        # ``retry-failures`` read it.
        kinds: dict[str, dict[str, int]] = {"failed": {}, "pending": {}}
        terminal = 0
        for state, code, count in errors:
            kind = failure_kind(code)[:80]
            kinds[state][kind] = kinds[state].get(kind, 0) + int(count)
            if state == "failed" and retry_class(code) == "terminal":
                terminal += int(count)
        return StoreStatus(
            int(meta["schema_version"]),
            int(meta["memory_epoch"]),
            int(meta["config_version"]),
            int(source_count),
            int(work_count),
            int(failed_count),
            int(leased_count),
            oldest,
            _most_frequent(kinds["failed"]),
            admission[0],
            admission[1],
            admission[2],
            terminal_failed_work=terminal,
            pending_error_counts=_most_frequent(kinds["pending"]),
        )

    def source(self, ref: str, revision: int) -> StoredSource | None:
        conn = self._check()
        if type(ref) is not str or not ref or len(ref) > 240 or type(revision) is not int or revision < 1:
            raise ContractError("INPUT_INVALID", "source_ref")

        if not allowed(self, "event", ref):
            return None
        loaded = self.remembered(
            ("source", ref, revision), lambda: self._source_row(conn, ref, revision), size=source_size
        )
        return None if loaded is None else stored_source(*loaded)

    def _source_row(self, conn, ref: str, revision: int):
        """The visible row of one source version and, for a part of a long message, how many parts of it are readable;
        ``None`` when it is not visible."""
        scopes = sorted(self.context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        row = conn.execute(
            f"""SELECT {",".join(SOURCE_COLUMNS)} FROM source_events WHERE event_id=? AND source_revision=? AND read_blocked=0 AND scope_id IN ({marks})
            AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)""",
            (ref, revision, *scopes, self.context.project_id, self.context.branch_id),
        ).fetchone()
        if row is None:
            return None
        count = None
        if "segment" in json.loads(row["extra_json"]):
            count = conn.execute(
                "SELECT count(*) FROM source_events WHERE source_group_key=? AND source_revision=? AND read_blocked=0",
                (row["source_group_key"], row["source_revision"]),
            ).fetchone()[0]
        return row, count

    def prefetch_sources(self, pairs) -> None:
        """Load these source versions into a read transaction's memory (``remembered``), with whether each is its
        group's newest version and whether ``visibility.allowed`` admits it: two statements and two rows a page,
        however many there are.  A write transaction loads nothing ahead."""
        if self.__memo is None:
            return
        conn = self._check()
        wanted = [
            (ref, revision)
            for ref, revision in dict.fromkeys(pairs)
            if type(ref) is str
            and ref
            and len(ref) <= 240
            and type(revision) is int
            and revision >= 1
            and ("source", ref, revision) not in self.__memo
        ]

        scopes = sorted(self.context.allowed_scope_ids)
        # The ``+`` keeps SQLite on the primary key: with a few scopes it started from the scope index, and read every
        # source of them, 0.24 s for a single pair on tianji's.
        for start in range(0, len(wanted), _PREFETCH_PAGE):
            page = wanted[start : start + _PREFETCH_PAGE]
            admitted = allowed_refs(self, "event", (ref for ref, _revision in page))
            fields = ",".join(f"'{column}',s.{column}" for column in SOURCE_COLUMNS)
            row = conn.execute(
                f"""SELECT json_group_array(json_object({fields},
                       'segment_count',CASE WHEN json_type(s.extra_json,'$.segment') IS NOT NULL THEN
                           (SELECT count(*) FROM source_events g WHERE g.source_group_key=s.source_group_key
                            AND g.source_revision=s.source_revision AND g.read_blocked=0) END,
                       'head',NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=s.source_group_key
                            AND newer.source_revision>s.source_revision)))
                    FROM source_events s WHERE (s.event_id,s.source_revision) IN ({",".join("(?,?)" for _ in page)})
                    AND +s.read_blocked=0 AND +s.scope_id IN ({",".join("?" for _ in scopes)})
                    AND (s.project_id IS NULL OR s.project_id=?) AND (s.branch_id IS NULL OR s.branch_id=?)""",
                (*(value for pair in page for value in pair), *scopes, self.context.project_id, self.context.branch_id),
            ).fetchone()
            found = {(item["event_id"], item["source_revision"]): item for item in json.loads(row[0])}
            for ref, revision in page:
                item = found.get((ref, revision))
                if ref not in admitted:
                    continue
                loaded = None if item is None else (item, item["segment_count"])
                self.remember(("source", ref, revision), loaded, size=source_size(loaded))
                if item is not None:
                    self.remember(("head", ref, revision), bool(item["head"]))

    def remembered(self, key: tuple, load, *, size=None):
        """``load()``'s answer for ``key``, loaded once in a read transaction.

        A read transaction reads one snapshot, so what it loaded stays true until it ends.  A recall loaded each of its
        evidence sources up to five times, three statements each: 16,222 statements for one of yuheng's questions, and
        in a busy Hermes gateway every statement waited for the GIL, so the recall ran past its deadline in every stage
        (3.7.7).  A write transaction changes what it reads and remembers nothing.
        """
        memo = self.__memo
        if memo is None:
            return load()
        if key in memo:
            return memo[key]
        value = load()
        self.remember(key, value, size=0 if size is None else size(value))
        return value

    def remember(self, key: tuple, value, *, size: int = 0) -> None:
        """Keep ``value`` for ``key`` in a read transaction's memory (``remembered``); nothing in a write transaction.

        A transaction that reads a whole store keeps no more than ``_MEMO_ENTRIES`` answers and ``_MEMO_BYTES`` of
        source text (``size``): past either, what it loads is used and not kept.
        """
        memo = self.__memo
        if memo is None or key in memo:
            return
        if len(memo) >= _MEMO_ENTRIES or self.__memo_bytes + size > _MEMO_BYTES:
            return
        memo[key] = value
        self.__memo_bytes += size

    @property
    def remembers(self) -> bool:
        """Whether this transaction keeps what it loads (``remembered``): a read transaction does."""
        return self.__memo is not None

    def knows(self, key: tuple) -> bool:
        """Whether a read transaction has already loaded ``key`` (``remembered``)."""
        return self.__memo is not None and key in self.__memo


#: A store this large is brought forward only by a caller with this much
#: budget: the 1109 step rebuilds the lexical index, 95 s for 5.2 million
#: rows on a 1.4 GB store, which no hook can carry and every worker pass can.
HEAVY_UPGRADE_BYTES = 100_000_000
HEAVY_UPGRADE_SECONDS = 60.0


def upgrade_fits(store_bytes: int, remaining_seconds: float | None) -> bool:
    """Whether an open with this budget may bring a store of this size forward."""
    return remaining_seconds is None or remaining_seconds >= HEAVY_UPGRADE_SECONDS or store_bytes < HEAVY_UPGRADE_BYTES


def _store_bytes(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]


#: How much of the store a connection reads through a memory map.  Each operation opens its own connection, and on a
#: shared store another process writes between any two recalls, so SQLite's own page cache never carries over: a hook
#: recall read every page it touched with a read call of its own, 151,000 of them (586 MiB) for a 3,800-character
#: prompt.  Through the map those pages come straight from the system's file cache.  On a copy of the shared store that
#: recall took 0.73-0.76 s instead of 1.20-1.76 s with the cache warm, and 1.64-1.69 s instead of 2.68-3.11 s with it
#: cold, the state meant to model a server's first recall after an idle stretch.  SQLite maps no more than the file
#: holds and at most its build's limit (2,147,418,112 bytes in Python's builds); the rest is read as before, and
#: writes are unchanged.  An I/O error on a mapped page ends the process, and a mapped file cannot shrink.
STORE_MMAP_BYTES = 8 << 30


def _ensure_wal(conn: sqlite3.Connection) -> None:
    """Keep the store in WAL mode, where readers and the writer coexist.

    Under the rollback journal a two-second read left a writer "database is
    locked" after its whole timeout; under WAL it commits in 20 ms.  The mode
    is persistent in the file, so this is one pragma read almost always.  It
    runs only on a store this code has verified as its own, outside any
    transaction (where SQLite allows the switch); a concurrent connection can
    make SQLite decline the switch, and the next writable open tries again.
    Backups (maintenance/backup.py) already write their snapshot in rollback
    mode, and every read-only open reads a WAL store in every file state.
    """
    if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")


class SQLiteStorage:
    def __init__(self, binding: InstanceBinding, *, timeout_seconds: float = 1.0, upgrade_on_open: bool = True) -> None:
        if not isinstance(binding, InstanceBinding):
            raise ContractError("IDENTITY_UNBOUND")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 <= timeout_seconds <= 30
        ):
            raise ContractError("INPUT_INVALID", "storage_timeout")
        if type(upgrade_on_open) is not bool:
            raise ContractError("INPUT_INVALID", "upgrade_on_open")
        self.__binding = binding
        self.timeout_seconds = float(timeout_seconds)
        #: A store left at an older known schema by a package upgrade is brought
        #: forward by the first transaction that opens it (``initialize``: one
        #: transaction, identity-verified, rolled back whole on failure), so
        #: ``pip install -U`` alone is enough.  The doctor turns this off: it
        #: reports a pending upgrade and never applies one.
        self.upgrade_on_open = upgrade_on_open
        self.__pending_close: list[sqlite3.Connection] = []

    @property
    def binding(self) -> InstanceBinding:
        return self.__binding

    @property
    def path(self) -> Path:
        return self.__binding.data_directory / "memory.sqlite3"

    def _path_check(self) -> None:
        # Reject any symlink/junction ancestor, including read-only opens.
        for path in (self.path, *self.path.parents):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ContractError("IDENTITY_UNBOUND", "data_directory")
        if _directory(self.binding.data_directory.resolve()) != _directory(self.binding.data_directory):
            raise ContractError("IDENTITY_UNBOUND", "data_directory")

    def _context_check(self, context: TrustedContext) -> None:
        if not isinstance(context, TrustedContext) or context.binding != self.binding:
            raise ContractError("IDENTITY_UNBOUND")
        if not context.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED")

    def _open(
        self, mode: TruthDatabaseMode, remaining_seconds: float | None = None, *, restoring: bool = False
    ) -> sqlite3.Connection:
        # A failed close cannot silently abandon an acquired writer lease. No
        # new connection is opened until the prior close succeeds.
        while self.__pending_close:
            pending = self.__pending_close[-1]
            try:
                pending.close()
            except BaseException as exc:
                raise ContractError("STORAGE_UNAVAILABLE", "connection_cleanup") from exc
            self.__pending_close.pop()
        self._path_check()
        restore_marker = self.binding.data_directory / "restore-required.json"
        if not restoring and (restore_marker.exists() or restore_marker.is_symlink()):
            raise ContractError("RESTORE_UNVERIFIED")
        timeout = self.timeout_seconds
        if remaining_seconds is not None:
            if (
                type(remaining_seconds) not in (int, float)
                or not math.isfinite(remaining_seconds)
                or remaining_seconds <= 0
            ):
                raise ContractError("DEADLINE_EXCEEDED")
            timeout = min(timeout, remaining_seconds)
        # The writer lease is taken without blocking and held for one
        # transaction, so writers in separate processes -- a host and its
        # worker, the entries of a shared store -- take turns.  A turn ends in
        # milliseconds; failing at once on one sent captures to the memory-only
        # retry (one in ten with three entries writing).  A writer waits for the
        # lease as SQLite waits for its own lock: within this timeout.
        deadline = time.monotonic() + timeout
        while True:
            try:
                conn = connect_truth_database(
                    self.path, mode=mode, timeout=max(0.0, deadline - time.monotonic()), isolation_level=None
                )
            except TruthWriterBusyError:
                if time.monotonic() + _LEASE_POLL_SECONDS >= deadline:
                    raise
                time.sleep(_LEASE_POLL_SECONDS)
                continue
            try:
                conn.execute(f"PRAGMA mmap_size={STORE_MMAP_BYTES}")
            except BaseException as exc:
                self._close(conn, exc)
                raise
            return conn

    def _verify(self, conn: sqlite3.Connection, *, expected_schema: int = SCHEMA_VERSION) -> None:
        if (
            conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or conn.execute("PRAGMA user_version").fetchone()[0] != expected_schema
        ):
            if stale_header_schema(conn) is not None:
                # The store is intact and records its schema; only the header was overwritten.
                raise ContractError("SCHEMA_UNSUPPORTED", "header_stale:run_upgrade_store")
            raise ContractError("SCHEMA_UNSUPPORTED")
        row = conn.execute("SELECT * FROM instance_meta WHERE singleton=1").fetchone()
        if row is None or row["schema_version"] != expected_schema:
            raise ContractError("SCHEMA_UNSUPPORTED")
        # A store from before 1110 has no kind column; it was a host's own store.
        kind = row["installation_kind"] if "installation_kind" in row.keys() else "local"
        if kind != self.binding.installation_kind:
            raise ContractError("IDENTITY_UNBOUND", "installation_kind")
        # Counted in one row, every transaction: read row by row, the shared store's 760 scopes cost a busy Hermes
        # gateway 3 s per transaction (``lexical_index.index_terms``).  A binding's scopes are a set.
        stored, held = conn.execute(
            "SELECT (SELECT count(*) FROM instance_scopes),"
            " (SELECT count(*) FROM instance_scopes WHERE scope_id IN (SELECT value FROM json_each(?)))",
            (json.dumps(sorted(self.binding.scope_ids), ensure_ascii=False),),
        ).fetchone()
        if kind == "local":
            if (row["agent_id"], row["installation_id"], row["data_directory"], row["test_mode"]) != (
                self.binding.agent_id,
                self.binding.installation_id,
                _directory(self.binding.data_directory),
                int(self.binding.test_mode),
            ):
                raise ContractError("IDENTITY_UNBOUND")
            if stored != held or held != len(self.binding.scope_ids):
                raise ContractError("IDENTITY_UNBOUND", "scope_binding")
            return
        # A shared store is its fixed id, not its directory: a copied store opens
        # nowhere until ``adopt`` records the new place.  Entries each bind a
        # subset of its scopes; the store grows as they attach.
        if (row["agent_id"], row["installation_id"], row["test_mode"]) != (
            self.binding.agent_id,
            self.binding.installation_id,
            int(self.binding.test_mode),
        ):
            raise ContractError("IDENTITY_UNBOUND")
        if row["data_directory"] != _directory(self.binding.data_directory):
            raise ContractError("IDENTITY_UNBOUND", "store_moved:run_adopt")
        if held != len(self.binding.scope_ids):
            raise ContractError("IDENTITY_UNBOUND", "scope_binding")

    def _close(self, conn: sqlite3.Connection, original: BaseException | None) -> None:
        try:
            conn.close()
        except BaseException as cleanup:
            self.__pending_close.append(conn)
            if original is None:
                raise
            _cleanup_error(original, cleanup, "close")

    def initialize(self) -> StoreStatus:
        conn = self._open("rwc")
        original = None
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version in UPGRADE_CHAIN:
                # Verified as this store's own before anything is written, and
                # switched to WAL first, so readers keep reading through a
                # long upgrade instead of waiting on the rollback journal.
                self._verify(conn, expected_schema=version)
                _ensure_wal(conn)
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
            if exists:
                if conn.execute("PRAGMA user_version").fetchone()[0] == 1105:
                    self._verify(conn, expected_schema=1105)
                    upgrade_1105(conn)
                if conn.execute("PRAGMA user_version").fetchone()[0] == 1106:
                    self._verify(conn, expected_schema=1106)
                    upgrade_1106(conn)
                if conn.execute("PRAGMA user_version").fetchone()[0] == 1107:
                    self._verify(conn, expected_schema=1107)
                    upgrade_1107(conn)
                if conn.execute("PRAGMA user_version").fetchone()[0] == 1108:
                    self._verify(conn, expected_schema=1108)
                    upgrade_1108(conn)
                if conn.execute("PRAGMA user_version").fetchone()[0] == 1109:
                    self._verify(conn, expected_schema=1109)
                    upgrade_1109(conn)
                self._verify(conn)
            else:
                if (
                    conn.execute("PRAGMA user_version").fetchone()[0] != 0
                    or conn.execute("PRAGMA application_id").fetchone()[0] != 0
                ):
                    raise ContractError("SCHEMA_UNSUPPORTED")
                for statement in STATEMENTS:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO instance_meta(singleton,agent_id,installation_id,data_directory,schema_version,test_mode,installation_kind) VALUES (1,?,?,?,?,?,?)",
                    (
                        self.binding.agent_id,
                        self.binding.installation_id,
                        _directory(self.binding.data_directory),
                        SCHEMA_VERSION,
                        int(self.binding.test_mode),
                        self.binding.installation_kind,
                    ),
                )
                conn.executemany(
                    "INSERT INTO instance_scopes(scope_id) VALUES (?)", [(s,) for s in sorted(self.binding.scope_ids)]
                )
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            _ensure_wal(conn)
        except BaseException as exc:
            original = exc
            try:
                if conn.in_transaction:
                    conn.rollback()
            except BaseException as cleanup:
                _cleanup_error(exc, cleanup, "rollback")
            raise
        finally:
            self._close(conn, original)
        context = TrustedContext(self.binding, "initialization", self.binding.scope_ids, "host_generated")
        with self.read(context) as tx:
            return tx.status()

    def adopt(self) -> str:
        """Record this binding's directory as where a copied shared store now lives.

        A shared store is its fixed id, so a copy opens nowhere until this runs:
        ``_verify`` refuses it with ``store_moved:run_adopt``.  Everything
        ``_verify`` checks is checked here except the directory, which is then
        written.  Returns the directory the store recorded before.  A local store
        is its directory and is never adopted.
        """
        if self.binding.installation_kind != "shared":
            raise ContractError("ACCESS_DENIED", "local_store")
        conn = self._open("rw")
        original = None
        try:
            if (
                conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
            ):
                raise ContractError("SCHEMA_UNSUPPORTED")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM instance_meta WHERE singleton=1").fetchone()
            if row is None or row["schema_version"] != SCHEMA_VERSION:
                raise ContractError("SCHEMA_UNSUPPORTED")
            if row["installation_kind"] != "shared":
                raise ContractError("IDENTITY_UNBOUND", "installation_kind")
            if (row["agent_id"], row["installation_id"], row["test_mode"]) != (
                self.binding.agent_id,
                self.binding.installation_id,
                int(self.binding.test_mode),
            ):
                raise ContractError("IDENTITY_UNBOUND")
            if not self.binding.scope_ids <= frozenset(
                r[0] for r in conn.execute("SELECT scope_id FROM instance_scopes")
            ):
                raise ContractError("IDENTITY_UNBOUND", "scope_binding")
            previous = row["data_directory"]
            conn.execute(
                "UPDATE instance_meta SET data_directory=? WHERE singleton=1",
                (_directory(self.binding.data_directory),),
            )
            conn.commit()
            return previous
        except BaseException as exc:
            original = exc
            try:
                if conn.in_transaction:
                    conn.rollback()
            except BaseException as cleanup:
                _cleanup_error(exc, cleanup, "rollback")
            raise
        finally:
            self._close(conn, original)

    @contextmanager
    def _transaction(
        self, context: TrustedContext, *, writable: bool, remaining_seconds: float | None, restoring: bool = False
    ) -> Iterator[Transaction]:
        self._context_check(context)
        conn = self._open("rw" if writable else "ro", remaining_seconds, restoring=restoring)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            upgrade = self.upgrade_on_open and not restoring and version in UPGRADE_CHAIN
            if upgrade:
                fits = upgrade_fits(_store_bytes(conn), remaining_seconds)
            elif writable and version == SCHEMA_VERSION:
                _ensure_wal(conn)
        except BaseException as exc:
            # Nothing below closes this connection yet.  Left open, a writable one kept the writer lease until the
            # process ended, and every other process's writes failed.  A busy store can answer
            # the first statement here with "database is locked".
            self._close(conn, exc)
            raise
        if upgrade:
            self._close(conn, None)
            if not fits:
                # A hook's few seconds cannot carry a rebuild that takes a
                # minute on a large store; the worker's pass or the installer
                # brings it forward, and the doctor names the pending step.
                raise ContractError("SCHEMA_UNSUPPORTED", "upgrade_pending")
            self.initialize()
            conn = self._open("rw" if writable else "ro", remaining_seconds, restoring=restoring)
        tx = Transaction(conn, context, writable=writable)
        original = None
        try:
            conn.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
            self._verify(conn)
            # A restore can establish its fence while this connection waits
            # for the writer lease. Recheck after acquiring the transaction.
            restore_marker = self.binding.data_directory / "restore-required.json"
            if not restoring and (restore_marker.exists() or restore_marker.is_symlink()):
                raise ContractError("RESTORE_UNVERIFIED")
            yield tx
            if writable:
                tx._assert_committable()
                conn.commit()
            else:
                conn.rollback()
        except BaseException as exc:
            original = exc
            try:
                if conn.in_transaction:
                    conn.rollback()
            except BaseException as cleanup:
                _cleanup_error(exc, cleanup, "rollback")
            raise
        finally:
            tx._finish()
            self._close(conn, original)

    def write(self, context: TrustedContext, *, remaining_seconds: float | None = None):
        return self._transaction(context, writable=True, remaining_seconds=remaining_seconds)

    def read(self, context: TrustedContext, *, remaining_seconds: float | None = None):
        return self._transaction(context, writable=False, remaining_seconds=remaining_seconds)
