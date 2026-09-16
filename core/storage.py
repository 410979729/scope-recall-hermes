"""The sole core SQLite transaction boundary. No host or Provider dependencies."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from ..contracts import ContractError, InstanceBinding, SourceEvent, TrustedContext, validate_capture
from .truth_connection import TruthDatabaseMode, connect_truth_database
from .schema import APPLICATION_ID, SCHEMA_VERSION, STATEMENTS, upgrade_1105, upgrade_1106, upgrade_1107
from .events import lexical_terms, prepare_capture, query_terms


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _directory(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


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
    work_error_counts: tuple[tuple[str, int], ...] = ()
    source_only_sources: int | None = None
    deferred_sources: int | None = None
    oldest_deferred_at: str | None = None


@dataclass(frozen=True)
class StoredSource:
    ref: str
    revision: int
    scope_id: str
    session_id: str
    project_id: str | None
    branch_id: str | None
    event: SourceEvent
    content_sha256: str
    suppressed: bool
    capture_gaps: tuple[str, ...] = ()
    import_provenance_sha256: str | None = None


@dataclass(frozen=True)
class SourceWrite:
    disposition: str
    ref: str
    revision: int


class Transaction:
    """Scoped repository operations; no public connection or SQL execution surface."""

    def __init__(self, connection: sqlite3.Connection, context: TrustedContext, *, writable: bool) -> None:
        self.__connection = connection
        self.context = context
        self.__writable = writable
        self.__active = True
        self.__poisoned = False
        self.__savepoint_sequence = 0

    def _check(self, *, write: bool = False) -> sqlite3.Connection:
        if not self.__active or self.__poisoned:
            raise ContractError("STORAGE_UNAVAILABLE", "transaction_closed")
        if write and not self.__writable:
            raise ContractError("ACCESS_DENIED", "read_only")
        return self.__connection

    @property
    def deletions(self):
        from .delete_storage import Deletions
        return Deletions(self)

    @property
    def episodes(self):
        from .episode_storage import Episodes
        return Episodes(self)

    @property
    def artifacts(self):
        from .artifact_storage import Artifacts
        return Artifacts(self)

    @property
    def references(self):
        from .reference_storage import References
        return References(self)

    def _finish(self) -> None:
        self.__active = False

    def _assert_committable(self) -> None:
        self._check(write=True)

    @property
    def claims(self):
        from .claim_storage import Claims
        self._check()
        return Claims(self)

    @property
    def work(self):
        from .work_storage import WorkItems
        self._check()
        return WorkItems(self)

    @property
    def candidates(self):
        from .candidate_storage import CandidateLifecycle
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
        return int(self._check().execute(
            "SELECT memory_epoch FROM instance_meta WHERE singleton=1"
        ).fetchone()[0])

    def status(self, *, include_all_projects: bool = False, include_admission: bool = False) -> StoreStatus:
        conn = self._check()
        meta = conn.execute("SELECT schema_version,memory_epoch,config_version FROM instance_meta WHERE singleton=1").fetchone()
        scopes = sorted(self.context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        context_filter = "AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)"
        params = (*scopes,self.context.project_id,self.context.branch_id)
        if include_all_projects:
            # Metadata-only installation diagnostics; scope isolation remains.
            context_filter, params = "", tuple(scopes)
        source_count = conn.execute(f"SELECT count(*) FROM source_events WHERE read_blocked=0 AND scope_id IN ({marks}) {context_filter}", params).fetchone()[0]
        work_count = conn.execute(f"SELECT count(*) FROM work_items WHERE state IN ('pending','leased') AND scope_id IN ({marks}) {context_filter}", params).fetchone()[0]
        failed_count = conn.execute(f"SELECT count(*) FROM work_items WHERE state='failed' AND scope_id IN ({marks}) {context_filter}", params).fetchone()[0]
        leased_count = conn.execute(f"SELECT count(*) FROM work_items WHERE state='leased' AND scope_id IN ({marks}) {context_filter}", params).fetchone()[0]
        oldest = conn.execute(f"""SELECT MIN(COALESCE((SELECT e.persisted_at FROM source_events e
            WHERE e.event_id=work_items.subject_ref AND e.source_revision=work_items.subject_revision),available_at))
            FROM work_items WHERE state IN ('pending','leased') AND scope_id IN ({marks}) {context_filter}""", params).fetchone()[0]
        # Detailed source processing counts are diagnostic-only; avoid a JSON
        # scan of all sources on every internal epoch/queue status read.
        admission = (None, None, None)
        if include_admission:
            admission = conn.execute(f"""SELECT
            COALESCE(SUM(json_extract(extra_json,'$._scope_recall_admission.disposition')='source_only'),0),
            COALESCE(SUM(json_extract(extra_json,'$._scope_recall_admission.disposition')='deferred'),0),
            MIN(CASE WHEN json_extract(extra_json,'$._scope_recall_admission.disposition')='deferred' THEN persisted_at END)
            FROM source_events WHERE read_blocked=0 AND suppressed=0 AND scope_id IN ({marks}) {context_filter}
            AND NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=source_events.source_group_key
                AND newer.source_revision>source_events.source_revision)
            AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                AND b.object_ref=source_events.event_id AND (b.read_blocked=1 OR b.suppressed=1))""", params).fetchone()
        errors = conn.execute(f"""SELECT last_error_code,COUNT(*) AS n FROM work_items
            WHERE state IN ('pending','failed') AND last_error_code IS NOT NULL
            AND scope_id IN ({marks}) {context_filter}
            GROUP BY last_error_code ORDER BY n DESC LIMIT 16""", params).fetchall()
        return StoreStatus(
            int(meta["schema_version"]),
            int(meta["memory_epoch"]),
            int(meta["config_version"]),
            int(source_count),
            int(work_count),
            int(failed_count), int(leased_count), oldest,
            tuple((str(row[0]).rsplit('|', 1)[-1][:80], int(row[1])) for row in errors),
            admission[0], admission[1], admission[2],
        )

    def source_by_event_key(self, source_event_key: str, revision: int = 1) -> StoredSource | None:
        """Resolve a trusted host occurrence without bypassing source visibility."""
        if type(source_event_key) is not str or not 1 <= len(source_event_key) <= 512 or not source_event_key.strip() or '\x00' in source_event_key:
            raise ContractError('INPUT_INVALID', 'source_event_key')
        identity = _json([self.context.binding.installation_id, source_event_key])
        source = self.source('event-' + hashlib.sha256(identity.encode('utf-8')).hexdigest(), revision)
        if source is not None:
            return source
        first_key = 'segmented-' + hashlib.sha256(source_event_key.encode('utf-8')).hexdigest() + '/0'
        identity = _json([self.context.binding.installation_id, first_key])
        source = self.source('event-' + hashlib.sha256(identity.encode('utf-8')).hexdigest(), revision)
        if source is not None and source.event.get('segment', {}).get('group_key') == source_event_key:
            return source
        return None

    def source(self, ref: str, revision: int) -> StoredSource | None:
        conn = self._check()
        if type(ref) is not str or not ref or len(ref) > 240 or type(revision) is not int or revision < 1:
            raise ContractError("INPUT_INVALID", "source_ref")
        from .visibility import allowed
        if not allowed(self,"event",ref):
            return None
        scopes = sorted(self.context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        row = conn.execute(f"""SELECT * FROM source_events WHERE event_id=? AND source_revision=? AND read_blocked=0 AND scope_id IN ({marks})
            AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)""",
            (ref, revision, *scopes,self.context.project_id,self.context.branch_id)).fetchone()
        if row is None:
            return None
        event = json.loads(row["extra_json"])
        event.pop("_scope_recall_admission", None)  # Internal scheduling never enters source evidence or model input.
        event.update(protocol_version="1.1", source_event_key=row["source_event_key"],
                     source_revision=row["source_revision"], origin=row["origin"], role=row["role"],
                     content=row["content"], occurred_at=row["occurred_at"], recorded_at=row["recorded_at"],
                     time_precision=row["time_precision"], capture_state=row["capture_state"])
        for name in ("source_original_origin", "dataset_id"):
            if row[name] is not None:
                event[name] = row[name]
        gaps = list(json.loads(row["capture_gaps_json"]))
        if "segment" in event:
            total = row["segment_total"]
            count = conn.execute("SELECT count(*) FROM source_events WHERE source_group_key=? AND source_revision=? AND read_blocked=0", (row["source_group_key"], row["source_revision"])).fetchone()[0]
            if total is None or count != total or event["segment"]["truncated"]:
                gaps.append("source_segments_incomplete")
        return StoredSource(row["event_id"], row["source_revision"], row["scope_id"], row["session_id"], row["project_id"], row["branch_id"], event, row["content_sha256"], bool(row["suppressed"]), tuple(dict.fromkeys(gaps)), row["import_provenance_sha256"])

    def source_current(self, ref: str) -> StoredSource | None:
        """Resolve the visible current source revision in one bounded lookup."""
        scopes = sorted(self.context.allowed_scope_ids)
        if not scopes:
            return None
        marks = ",".join("?" for _ in scopes)
        row = self._check().execute(f"""SELECT max(source_revision) AS revision FROM source_events
            WHERE event_id=? AND read_blocked=0 AND scope_id IN ({marks})
            AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)""",
            (ref, *scopes, self.context.project_id, self.context.branch_id)).fetchone()
        if row is None or row["revision"] is None:
            return None
        return self.source(ref, int(row["revision"]))


    def _admitted_source(self, event: SourceEvent) -> dict:
        """Validate a capture against the contract, the import provenance and the
        admission policy; the stored event must be exactly the admitted one."""
        event = validate_capture(dict(event), self.context)
        provenance = self.context.import_provenance
        if provenance is not None:
            from ..contracts import import_source_fingerprint
            if event.get("source_original_origin") != provenance.original_origin or import_source_fingerprint(event) not in provenance.source_fingerprints:
                raise ContractError("ACCESS_DENIED", "import_provenance")
        admitted = prepare_capture(event, self.context)
        if admitted.rejection or len(admitted.events) != 1 or admitted.events[0] != event:
            raise ContractError("INPUT_INVALID", "unprepared_source")
        return event

    def _same_identity(self, row, scope_id: str) -> bool:
        return (row["scope_id"], row["session_id"], row["project_id"], row["branch_id"]) == (
            scope_id, self.context.session_id, self.context.project_id, self.context.branch_id)

    def _check_source_group(self, conn, group_key: str, scope_id: str, revision: int, segment_total: int):
        """A segment group belongs to one identity and one segment count; a
        blocked group refuses new members.  Returns the group's block policy row."""
        from .delete_storage import group_digest
        digest = group_digest(self.context.binding, scope_id, self.context.project_id, self.context.branch_id, group_key)
        policy = conn.execute("SELECT read_blocked,suppressed FROM source_group_blocks WHERE group_sha256=?", (digest,)).fetchone()
        if policy is not None and policy["read_blocked"]:
            raise ContractError("ACCESS_DENIED", "source_unavailable")
        for row in conn.execute("SELECT scope_id,session_id,project_id,branch_id,source_revision,segment_total,read_blocked FROM source_events WHERE source_group_key=?", (group_key,)):
            if row["read_blocked"]:
                raise ContractError("ACCESS_DENIED", "source_unavailable")
            if not self._same_identity(row, scope_id):
                raise ContractError("VERSION_CONFLICT", "source_group_identity")
            if row["source_revision"] == revision and row["segment_total"] != segment_total:
                raise ContractError("VERSION_CONFLICT", "source_segment_total")
        return policy

    def _existing_revision(self, conn, ref: str, scope_id: str, revision: int, fingerprint: str) -> bool:
        """Whether this exact revision is already stored.  A different identity or a
        different fingerprint under the same revision is a conflict, not a retry."""
        for row in conn.execute("SELECT source_revision,event_sha256,scope_id,session_id,project_id,branch_id,read_blocked FROM source_events WHERE event_id=?", (ref,)):
            if row["read_blocked"]:
                raise ContractError("ACCESS_DENIED", "source_unavailable")
            if not self._same_identity(row, scope_id):
                raise ContractError("VERSION_CONFLICT", "source_identity")
            if row["source_revision"] == revision:
                if row["event_sha256"] != fingerprint:
                    raise ContractError("VERSION_CONFLICT", "source_revision")
                return True
        return False

    def _inherits_suppression(self, conn, scope_id: str, content: str) -> bool:
        """A source restating a suppressed claim (subject, predicate, value and every
        condition literally present) is suppressed with it."""
        return conn.execute("""SELECT 1 FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
            WHERE c.suppressed=1 AND c.read_blocked=0 AND c.scope_id=? AND c.project_id IS ? AND c.branch_id IS ?
            AND v.state IN ('active','disputed') AND instr(?,c.subject)>0 AND instr(?,c.predicate)>0
            AND instr(?,json_extract(v.payload_json,'$.value_text'))>0
            AND NOT EXISTS(SELECT 1 FROM json_each(v.payload_json,'$.conditions') WHERE instr(?,value)=0) LIMIT 1""",
            (scope_id, self.context.project_id, self.context.branch_id, content, content, content, content)).fetchone() is not None

    def put_source(self, event: SourceEvent, *, scope_id: str, persisted_at: str, capture_gaps: tuple[str, ...] = ()) -> SourceWrite:
        conn = self._check(write=True)
        self._scope(scope_id)
        event = self._admitted_source(event)
        provenance = self.context.import_provenance
        # First delivery's recorded_at is retained.  Transport retries may arrive
        # later; occurrence time and all provenance/content fields must agree.
        identity = _json([self.context.binding.installation_id, event["source_event_key"]])
        ref = "event-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        from .visibility import allowed
        if not allowed(self, "event", ref):
            raise ContractError("ACCESS_DENIED", "source_unavailable")
        revision = event["source_revision"]
        fingerprint_input = {k: v for k, v in event.items() if k != "recorded_at"}
        provenance_hash = provenance.manifest_sha256 if provenance else None
        fingerprint = hashlib.sha256(_json([scope_id, self.context.session_id, self.context.project_id, self.context.branch_id, fingerprint_input, provenance_hash]).encode("utf-8")).hexdigest()
        segment = event.get("segment")
        group_key = segment["group_key"] if segment else event["source_event_key"]
        segment_index, segment_total = (segment["index"], segment["total"]) if segment else (0, 1)
        group_policy = self._check_source_group(conn, group_key, scope_id, revision, segment_total)
        if self._existing_revision(conn, ref, scope_id, revision, fingerprint):
            return SourceWrite("duplicate", ref, revision)
        columns = ("source_event_key", "origin", "role", "content", "occurred_at", "recorded_at", "time_precision", "capture_state")
        extras = {k: v for k, v in event.items() if k not in {*columns, "protocol_version", "source_revision", "source_original_origin", "dataset_id"}}
        conn.execute("""INSERT INTO source_events(event_id,source_revision,scope_id,session_id,project_id,branch_id,
            source_event_key,origin,role,content,occurred_at,recorded_at,time_precision,capture_state,
            content_sha256,event_sha256,persisted_at,source_original_origin,dataset_id,extra_json,
            source_group_key,segment_index,segment_total,capture_gaps_json,import_provenance_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (ref, revision, scope_id, self.context.session_id, self.context.project_id, self.context.branch_id,
            *(event[k] for k in columns), hashlib.sha256(event["content"].encode("utf-8")).hexdigest(), fingerprint, persisted_at,
            event.get("source_original_origin"), event.get("dataset_id"), _json(extras), group_key, segment_index, segment_total, _json(capture_gaps), provenance_hash))
        if (group_policy is not None and group_policy["suppressed"]) or self._inherits_suppression(conn, scope_id, event["content"]):
            conn.execute("UPDATE source_events SET suppressed=1 WHERE event_id=? AND source_revision=?", (ref, revision))
        conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        return SourceWrite("inserted", ref, revision)

    def index_source(self, ref: str, revision: int) -> None:
        conn = self._check(write=True)
        source = self.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        conn.executemany("INSERT INTO lexical_projection(term,event_id,source_revision) VALUES (?,?,?) ON CONFLICT DO NOTHING", [(term, ref, revision) for term in lexical_terms(source.event["content"])])

    def source_projection_status(self, ref: str, revision: int) -> tuple[str, str]:
        conn = self._check()
        source = self.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        actual = tuple(r[0] for r in conn.execute("SELECT term FROM lexical_projection WHERE event_id=? AND source_revision=? ORDER BY term", (ref, revision)))
        lexical = "ready" if actual == lexical_terms(source.event["content"]) else "not_ready"
        work = conn.execute("SELECT state FROM work_items WHERE work_type='embed' AND subject_ref=? AND subject_revision=?", (ref, revision)).fetchone()
        semantic = "not_scheduled" if work is None else {"pending":"pending", "leased":"pending", "done":"ready", "failed":"failed", "obsolete":"obsolete"}[work[0]]
        return lexical, semantic

    def search_sources(self, query: str, *, limit: int = 20, history: bool = False, automatic: bool = False) -> tuple[StoredSource, ...]:
        conn = self._check()
        if type(limit) is not int or not 1 <= limit <= 200 or type(history) is not bool or type(automatic) is not bool:
            raise ContractError("INPUT_INVALID", "search_limit")
        terms = query_terms(query)
        if not terms:
            return ()
        scopes = sorted(self.context.allowed_scope_ids)
        term_marks = ",".join("?" for _ in terms)
        scope_marks = ",".join("?" for _ in scopes)
        current = "" if history else "AND NOT EXISTS (SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)"
        suppression = "AND e.suppressed=0 AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event' AND b.object_ref=e.event_id AND b.suppressed=1)" if automatic else ""
        rows = conn.execute(f"""SELECT e.event_id,e.source_revision,count(*) AS hits FROM lexical_projection p
            JOIN source_events e ON e.event_id=p.event_id AND e.source_revision=p.source_revision
            WHERE p.term IN ({term_marks}) AND e.scope_id IN ({scope_marks}) AND e.read_blocked=0
            AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)
            AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event' AND b.object_ref=e.event_id AND b.read_blocked=1)
            {current} {suppression}
            GROUP BY e.event_id,e.source_revision
            ORDER BY hits DESC,e.occurred_at DESC,e.event_id,e.source_revision DESC LIMIT ?""", (*terms, *scopes,self.context.project_id,self.context.branch_id,limit)).fetchall()
        return tuple(source for row in rows if (source := self.source(row["event_id"], row["source_revision"])) is not None)

    def enqueue_source(self, ref: str, revision: int, *, work_type: str, available_at: str) -> None:
        conn = self._check(write=True)
        if work_type not in {"consolidate", "embed"}:
            raise ContractError("INPUT_INVALID", "work_type")
        source = self.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        conn.execute("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(work_type,subject_ref,subject_revision) DO NOTHING""",
            (work_type,ref,revision,source.scope_id,source.project_id,source.branch_id,available_at))


class SQLiteStorage:
    def __init__(self, binding: InstanceBinding, *, timeout_seconds: float = 1.0) -> None:
        if not isinstance(binding, InstanceBinding):
            raise ContractError("IDENTITY_UNBOUND")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30:
            raise ContractError("INPUT_INVALID", "storage_timeout")
        self.__binding = binding
        self.timeout_seconds = float(timeout_seconds)
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

    def _open(self, mode: TruthDatabaseMode, remaining_seconds: float | None = None, *, restoring: bool = False) -> sqlite3.Connection:
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
            if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
                raise ContractError("DEADLINE_EXCEEDED")
            timeout = min(timeout, remaining_seconds)
        return connect_truth_database(self.path, mode=mode, timeout=timeout, isolation_level=None)

    def _verify(self, conn: sqlite3.Connection, *, expected_schema: int = SCHEMA_VERSION) -> None:
        if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or conn.execute("PRAGMA user_version").fetchone()[0] != expected_schema:
            raise ContractError("SCHEMA_UNSUPPORTED")
        row = conn.execute("SELECT * FROM instance_meta WHERE singleton=1").fetchone()
        if row is None or row["schema_version"] != expected_schema:
            raise ContractError("SCHEMA_UNSUPPORTED")
        if (row["agent_id"],row["installation_id"],row["data_directory"],row["test_mode"]) != (self.binding.agent_id,self.binding.installation_id,_directory(self.binding.data_directory),int(self.binding.test_mode)):
            raise ContractError("IDENTITY_UNBOUND")
        scopes = frozenset(r[0] for r in conn.execute("SELECT scope_id FROM instance_scopes"))
        if scopes != self.binding.scope_ids:
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
                self._verify(conn)
            else:
                if conn.execute("PRAGMA user_version").fetchone()[0] != 0 or conn.execute("PRAGMA application_id").fetchone()[0] != 0:
                    raise ContractError("SCHEMA_UNSUPPORTED")
                for statement in STATEMENTS:
                    conn.execute(statement)
                conn.execute("INSERT INTO instance_meta(singleton,agent_id,installation_id,data_directory,schema_version,test_mode) VALUES (1,?,?,?,?,?)", (self.binding.agent_id,self.binding.installation_id,_directory(self.binding.data_directory),SCHEMA_VERSION,int(self.binding.test_mode)))
                conn.executemany("INSERT INTO instance_scopes(scope_id) VALUES (?)", [(s,) for s in sorted(self.binding.scope_ids)])
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
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

    @contextmanager
    def _transaction(self, context: TrustedContext, *, writable: bool, remaining_seconds: float | None, restoring: bool = False) -> Iterator[Transaction]:
        self._context_check(context)
        conn = self._open("rw" if writable else "ro", remaining_seconds,restoring=restoring)
        tx = Transaction(conn, context, writable=writable)
        original = None
        try:
            conn.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
            self._verify(conn)
            # A restore can establish its fence while this connection waits
            # for the writer lease. Recheck after acquiring the transaction.
            restore_marker = self.binding.data_directory / 'restore-required.json'
            if not restoring and (restore_marker.exists() or restore_marker.is_symlink()):
                raise ContractError('RESTORE_UNVERIFIED')
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
