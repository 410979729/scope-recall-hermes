"""Bring an entry's own store into the shared store it attached to.

``attach`` moves a home's own store aside and starts it on the shared store,
which does not see what the home remembered before.  ``import-entry`` copies
that store's truth into the shared store as the entry's: every source, fact,
episode, reference, candidate, deletion and block, in one transaction, with the
source store opened read-only.  SQLite is the authority; the vectors are
derived, so none are copied.  The import queues, for the shared worker, the
embeddings the source store had: every source it embedded whose vector its
retention had not expired, and every fact's current version.  Until the
worker has made them, those memories are found by their words.

Three things cannot be copied as they are.

* Source ids are derived from the store's keys, and legacy migrations gave
  different stores the same keys for different content: three pilot stores
  shared 13,073 ids.  Every imported source gets an id of its own,
  ``event-sha256(["import", entry, old id])``, and every reference to it --
  columns, JSON and the work that names it -- is rewritten; keys and group keys
  take the prefix ``import:<entry>:``, and sessions ``<entry>:``, as a live
  capture's do.  An id is renamed where the old store has it, whatever its
  form: 85,112 of the pilot's sources were ``event-legacy-<32 hex>``, which
  3.2.0rc3 renamed in columns but not in JSON or queued work.
* Integer ids local to a store -- ``source_id``, episode sequences, lexical
  term ids, authorization payload ids -- are renumbered.
* A group block's digest includes the installation id; it is recomputed for
  the shared store, so a forgotten conversation stays forgotten.

A fact whose slot the shared store already fills is not imported, nor are its
candidate rows: the first store imported keeps the slot, and the receipt names
what was left out.  Its sources are imported like every other.  Work history is not copied; pending
work is, as new work.  A store already imported for the entry is refused.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from urllib.request import pathname2url

from ..adapters.hermes.installation import read_shared_payload
from ..core.delete_storage import group_digest
from ..core.schema import SCHEMA_VERSION
from ..core.truth_connection import connect_truth_database
from ..core.writer_lease import TruthWriterBusyError
from ..runtime.running_code import live_records

#: Store versions whose tables this import reads.  An older store is upgraded first.
SOURCE_SCHEMAS = frozenset({1109, 1110})
#: Anything shaped like a source id; renamed only when the old store holds exactly that id.
_EVENT_TOKEN = re.compile(r"(?<![0-9A-Za-z_-])event-[0-9A-Za-z_-]+")
_CHUNK = 2000


class SourceRefused(Exception):
    """The source store cannot be imported; nothing was written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Names:
    """The imported store's names in the shared store: one entry's namespace."""

    def __init__(self, entry_id: str, known: frozenset[str] = frozenset()) -> None:
        self.entry_id = entry_id
        self.prefix = f"import:{entry_id}:"
        #: The source ids the old store holds: the only text renamed.
        self.known = known
        self._events: dict[str, str] = {}

    def event(self, ref: str) -> str:
        new = self._events.get(ref)
        if new is None:
            digest = hashlib.sha256(_canonical(["import", self.entry_id, ref]).encode("utf-8")).hexdigest()
            new = self._events[ref] = f"event-{digest}"
        return new

    def ref(self, kind: str, ref: str) -> str:
        return self.event(ref) if kind == "event" else ref

    def text(self, value: str | None) -> str | None:
        """``value`` with every source id of the old store in it renamed; JSON stays valid.

        A token is renamed only when it is exactly an id the old store holds,
        so ``event-driven`` in a quoted value, or an id the old store no longer
        has, is left as it was.  Content is never passed here.
        """
        if value is None:
            return None
        return _EVENT_TOKEN.sub(lambda m: self.event(m.group(0)) if m.group(0) in self.known else m.group(0), value)

    def key(self, key: str) -> str:
        return self.prefix + key

    def session(self, session_id: str) -> str:
        return f"{self.entry_id}:{session_id}"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Iterator[sqlite3.Row]:
    cursor = conn.execute(sql, params)
    while True:
        batch = cursor.fetchmany(_CHUNK)
        if not batch:
            return
        yield from batch


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]], *, verb: str = "INSERT") -> int:
    if not rows:
        return 0
    columns = list(rows[0])
    marks = ",".join("?" for _ in columns)
    conn.executemany(f"{verb} INTO {table}({','.join(columns)}) VALUES ({marks})",
                     [tuple(row[c] for c in columns) for row in rows])
    return len(rows)


def _copy(conn, src, table: str, change: Callable[[dict[str, Any]], dict[str, Any] | None], *, verb: str = "INSERT",
          order: str = "") -> int:
    """Stream ``table`` from the source store through ``change`` (``None`` leaves a row out)."""
    written, batch = 0, []
    for row in _rows(src, f"SELECT * FROM {table} {order}"):
        changed = change(dict(row))
        if changed is not None:
            batch.append(changed)
        if len(batch) >= _CHUNK:
            written += _insert(conn, table, batch, verb=verb)
            batch = []
    return written + _insert(conn, table, batch, verb=verb)


def _open_source(database: Path, home: str) -> sqlite3.Connection:
    src = sqlite3.connect(f"file:{pathname2url(str(database))}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    version = src.execute("PRAGMA user_version").fetchone()[0]
    meta = src.execute("SELECT * FROM instance_meta WHERE singleton=1").fetchone()
    if version not in SOURCE_SCHEMAS or meta is None or meta["schema_version"] != version:
        raise SourceRefused(f"source schema {version} is not one this import reads; run upgrade-store on it first")
    kind = meta["installation_kind"] if "installation_kind" in meta.keys() else "local"
    if kind != "local":
        raise SourceRefused("the source is itself a shared store")
    if os.path.normcase(os.path.normpath(os.path.dirname(meta["data_directory"]))) != os.path.normcase(os.path.normpath(home)):
        raise SourceRefused(f"the source was the store of {os.path.dirname(meta['data_directory'])}, not of this entry's home {home}")
    if src.execute("SELECT count(*) FROM capture_inbox").fetchone()[0]:
        raise SourceRefused("the source has captures waiting in its inbox; open it once with its own host first")
    return src


def _import_rows(conn, src, names: _Names, *, store_installation: str, store_scopes: frozenset[str], now: str) -> dict:
    counts: dict[str, int] = {}
    source_meta = src.execute("SELECT installation_id FROM instance_meta").fetchone()
    old_binding = SimpleNamespace(installation_id=source_meta[0])
    new_binding = SimpleNamespace(installation_id=store_installation)

    scopes = [row[0] for row in src.execute("SELECT scope_id FROM instance_scopes")]
    conn.executemany("INSERT OR IGNORE INTO instance_scopes(scope_id) VALUES (?)", [(s,) for s in scopes])
    counts["scopes_without_entry"] = len(set(scopes) - store_scopes)

    auth: dict[int, int] = {}
    for old_id, payload in src.execute("SELECT authorization_id, payload FROM authorization_payloads"):
        conn.execute("INSERT OR IGNORE INTO authorization_payloads(payload) VALUES (?)", (payload,))
        auth[old_id] = conn.execute("SELECT authorization_id FROM authorization_payloads WHERE payload=?", (payload,)).fetchone()[0]

    source_offset = conn.execute("SELECT coalesce(max(source_id),0) FROM source_events").fetchone()[0]
    groups: dict[str, str] = {}

    def source(row):
        extra = json.loads(row["extra_json"])
        segment = extra.get("segment")
        if isinstance(segment, dict) and isinstance(segment.get("group_key"), str):
            segment["group_key"] = names.key(segment["group_key"])
        old = row["source_group_key"], row["scope_id"], row["project_id"], row["branch_id"]
        new_group = names.key(row["source_group_key"])
        groups[group_digest(old_binding, old[1], old[2], old[3], old[0])] = group_digest(new_binding, old[1], old[2], old[3], new_group)
        row.update(event_id=names.event(row["event_id"]), source_event_key=names.key(row["source_event_key"]),
                   source_group_key=new_group, session_id=names.session(row["session_id"]),
                   extra_json=names.text(_canonical(extra)), entry_id=names.entry_id,
                   source_id=None if row["source_id"] is None else row["source_id"] + source_offset)
        return row

    counts["sources"] = _copy(conn, src, "source_events", source)
    counts["source_authorizations"] = _copy(conn, src, "source_authorizations", lambda r: {
        **r, "event_id": names.event(r["event_id"]), "authorization_id": auth[r["authorization_id"]]})

    skipped: list[dict[str, str]] = []
    left_out: set[str] = set()
    for claim in _rows(src, "SELECT * FROM claims"):
        held = conn.execute("SELECT claim_id FROM claims WHERE slot_key=? OR claim_id=?",
                            (claim["slot_key"], claim["claim_id"])).fetchone()
        if held is not None:
            left_out.add(claim["claim_id"])
            skipped.append({"claim_id": claim["claim_id"], "kept": held[0], "kind": claim["kind"]})
    counts["claims"] = _copy(conn, src, "claims", lambda r: None if r["claim_id"] in left_out else r)
    counts["claim_versions"] = _copy(conn, src, "claim_versions", lambda r: None if r["claim_id"] in left_out else {
        **r, "payload_json": names.text(r["payload_json"])})
    counts["claims_left_out"] = len(left_out)

    sequence_offset = conn.execute("SELECT coalesce(max(sequence),0) FROM episode_events").fetchone()[0]
    renamed_episodes = 0

    def episode(row):
        nonlocal renamed_episodes
        taken = conn.execute("SELECT 1 FROM episodes WHERE anchor_key=? OR (series_key=? AND segment_index=?)",
                             (row["anchor_key"], row["series_key"], row["segment_index"])).fetchone()
        if taken is not None:
            renamed_episodes += 1
            row.update(anchor_key=names.key(row["anchor_key"]), series_key=names.key(row["series_key"]))
        return row

    counts["episodes"] = _copy(conn, src, "episodes", episode)
    counts["episodes_rekeyed"] = renamed_episodes
    counts["episode_versions"] = _copy(conn, src, "episode_versions", lambda r: {
        **r, "resume_json": names.text(r["resume_json"]),
        "processed_sequence": r["processed_sequence"] + sequence_offset if r["processed_sequence"] else 0})
    counts["episode_events"] = _copy(conn, src, "episode_events", lambda r: {
        **r, "sequence": r["sequence"] + sequence_offset, "source_ref": names.event(r["source_ref"])})

    counts["artifacts"] = _copy(conn, src, "artifacts", lambda r: r)
    counts["artifact_versions"] = _copy(conn, src, "artifact_versions", lambda r: {
        **r, "blob_json": names.text(r["blob_json"]), "description_json": names.text(r["description_json"])})
    counts["reference_bindings"] = _copy(conn, src, "reference_bindings", lambda r: r)
    counts["reference_versions"] = _copy(conn, src, "reference_versions", lambda r: {
        **r, "payload_json": names.text(r["payload_json"])})

    def linked(row):
        if row["object_kind"] == "claim" and row["object_ref"] in left_out:
            return None
        return {**row, "object_ref": names.ref(row["object_kind"], row["object_ref"]),
                "source_ref": names.event(row["source_ref"])}

    counts["evidence_links"] = _copy(conn, src, "evidence_links", linked)
    counts["object_dependencies"] = _copy(conn, src, "object_dependencies", lambda r: {
        **r, "object_ref": names.ref(r["object_kind"], r["object_ref"]),
        "dependency_ref": names.ref(r["dependency_kind"], r["dependency_ref"])})
    counts["unresolved_updates"] = _copy(conn, src, "unresolved_updates", lambda r: {
        **r, "source_ref": names.event(r["source_ref"]), "candidate_refs_json": names.text(r["candidate_refs_json"])})

    # A candidate is a claim version under review: one left out takes its candidate rows with it.
    counts["candidate_lifecycle"] = _copy(conn, src, "candidate_lifecycle", lambda r: None if r["candidate_ref"] in left_out else r)
    counts["candidate_trigger_terms"] = _copy(conn, src, "candidate_trigger_terms",
                                              lambda r: None if r["candidate_ref"] in left_out else r)
    counts["candidate_evidence"] = _copy(conn, src, "candidate_evidence", lambda r: None if r["candidate_ref"] in left_out else {
        **r, "source_ref": names.event(r["source_ref"])})
    counts["candidate_source_triggers"] = _copy(conn, src, "candidate_source_triggers", lambda r: {
        **r, "source_ref": names.event(r["source_ref"])})
    counts["candidate_evaluations"] = _copy(conn, src, "candidate_evaluations", lambda r: None if r["candidate_ref"] in left_out else {
        **{k: v for k, v in r.items() if k != "evaluation_id"},
        "evidence_refs_json": names.text(r["evidence_refs_json"]), "work_id": None}, order="ORDER BY evaluation_id")

    counts["deletion_operations"] = _copy(conn, src, "deletion_operations", lambda r: {
        **r, "requested_refs_json": names.text(r["requested_refs_json"]),
        "expected_revisions_json": names.text(r["expected_revisions_json"]), "layers_json": names.text(r["layers_json"])})
    counts["deletion_members"] = _copy(conn, src, "deletion_members", lambda r: None
                                       if r["object_kind"] == "claim" and r["object_ref"] in left_out
                                       else {**r, "object_ref": names.ref(r["object_kind"], r["object_ref"])})
    counts["object_blocks"] = _copy(conn, src, "object_blocks", lambda r: None
                                    if r["object_kind"] == "claim" and r["object_ref"] in left_out
                                    else {**r, "object_ref": names.ref(r["object_kind"], r["object_ref"])})
    counts["restored_absence_blocks"] = _copy(conn, src, "restored_absence_blocks",
                                              lambda r: None if r["object_ref"] in left_out else r)
    orphans = 0

    def group_block(row):
        nonlocal orphans
        digest = groups.get(row["group_sha256"])
        if digest is None:
            orphans += 1
            return None
        return {**row, "group_sha256": digest}

    counts["source_group_blocks"] = _copy(conn, src, "source_group_blocks", group_block)
    counts["group_blocks_without_sources"] = orphans
    counts["expired_vectors"] = _copy(conn, src, "expired_vectors", lambda r: {
        **r, "source_ref": names.event(r["source_ref"])})

    conn.execute("CREATE TEMP TABLE import_terms(old_id INTEGER PRIMARY KEY, term TEXT NOT NULL)")
    cursor = src.execute("SELECT term_id, term FROM lexical_terms")
    while batch := cursor.fetchmany(_CHUNK):
        conn.executemany("INSERT INTO temp.import_terms(old_id, term) VALUES (?,?)", batch)
    conn.execute("INSERT OR IGNORE INTO lexical_terms(term) SELECT term FROM temp.import_terms")
    terms = dict(conn.execute("SELECT i.old_id, t.term_id FROM temp.import_terms i JOIN lexical_terms t ON t.term=i.term"))
    conn.execute("DROP TABLE temp.import_terms")
    counts["lexical_postings"] = _copy(conn, src, "lexical_postings", lambda r: None if r["term_id"] not in terms else {
        "term_id": terms[r["term_id"]], "source_id": r["source_id"] + source_offset}, verb="INSERT OR IGNORE")

    expired = {(ref, revision) for ref, revision in src.execute("SELECT source_ref, source_revision FROM expired_vectors")}

    def pending(row):
        """Pending work, as new work; and a source's embedding again wherever the source store had one.

        Work naming a source the old store no longer holds is left behind: nothing it could do remains.
        """
        subject = row["subject_ref"]
        source = subject.startswith("event-")
        if row["scope_id"] not in store_scopes or subject in left_out or (source and subject not in names.known):
            return None
        embedded = (row["work_type"] == "embed" and source and row["state"] != "obsolete"
                    and (subject, row["subject_revision"]) not in expired)
        if not embedded and row["state"] not in ("pending", "leased"):
            return None
        return {"work_type": row["work_type"], "subject_ref": names.event(subject) if source else subject,
                "subject_revision": row["subject_revision"], "scope_id": row["scope_id"], "project_id": row["project_id"],
                "branch_id": row["branch_id"], "available_at": now}

    counts["work_queued"] = _copy(conn, src, "work_items", pending, verb="INSERT OR IGNORE")
    # Every claim head is queued for the vector index, as accepting it would have (claim_storage).
    heads = [(c["claim_id"], c["current_revision"], c["scope_id"], c["project_id"], c["branch_id"], now)
             for c in _rows(src, "SELECT claim_id,current_revision,scope_id,project_id,branch_id FROM claims")
             if c["claim_id"] not in left_out and c["scope_id"] in store_scopes]
    conn.executemany("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
                        VALUES ('embed',?,?,?,?,?,?) ON CONFLICT(work_type,subject_ref,subject_revision) DO NOTHING""", heads)
    counts["claim_embeddings_queued"] = len(heads)
    conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
    return {"counts": counts, "claims_left_out": skipped}


def import_entry(*, root: Path, entry_id: str, source: Path, dry_run: bool = False,
                 now: str | None = None) -> dict[str, Any]:
    from .shared import SharedStoreError, _Run

    now = now or _now()
    payload = read_shared_payload(root)
    record = next((e for e in payload["entries"] if e["entry_id"] == entry_id and not e.get("detached_at")), None)
    if record is None:
        raise SharedStoreError(f"{entry_id} is not an attached entry of this store")
    database = source / "memory.sqlite3" if source.is_dir() else source
    target = root / "memory.sqlite3"
    if not database.is_file() or not target.is_file():
        raise SharedStoreError("no memory.sqlite3 at --from, or the shared store has none yet")
    if database.resolve() == target.resolve():
        raise SharedStoreError("--from is the shared store itself")
    if live_records(database.parent):
        raise SharedStoreError("a process still has the source store open; stop it first")
    started = time.monotonic()
    result: dict[str, Any] = {"root": str(root), "entry_id": entry_id, "source": str(database), "dry_run": dry_run}
    try:
        src = _open_source(database, record["home"])
        names = _Names(entry_id, frozenset(row[0] for row in src.execute("SELECT event_id FROM source_events")))
    except (SourceRefused, sqlite3.Error) as exc:
        raise SharedStoreError(f"source refused: {exc}") from None
    try:
        try:
            conn = connect_truth_database(target, mode="rw", timeout=30.0, isolation_level=None)
        except TruthWriterBusyError:
            raise SharedStoreError("the shared store is being written; stop every entry's host and the worker first") from None
        try:
            meta = conn.execute("SELECT * FROM instance_meta WHERE singleton=1").fetchone()
            if (conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION or meta["installation_kind"] != "shared"
                    or meta["installation_id"] != payload["installation_id"]):
                raise SharedStoreError("the store at --root is not the shared store its manifest names")
            conn.execute("BEGIN IMMEDIATE")
            done = conn.execute("SELECT 1 FROM source_events WHERE source_event_key>=? AND source_event_key<? LIMIT 1",
                                (names.prefix, names.prefix[:-1] + ";")).fetchone()
            if done is None:
                # One transaction holds the writer lease for as long as the import takes: a host
                # writing meanwhile would wait past its capture bound.
                if live_records(root):
                    raise SharedStoreError("a host or the worker still has the shared store open; stop them first")
                try:
                    result.update(_import_rows(conn, src, names, store_installation=payload["installation_id"],
                                               store_scopes=frozenset(payload["scope_ids"]), now=now))
                except sqlite3.IntegrityError as exc:
                    # Only sources are renamed; any other id the store already holds is refused, not guessed at.
                    raise SharedStoreError(f"the source cannot be imported as it is: {exc}") from None
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise SharedStoreError(f"import would break {len(broken)} references (first: {tuple(broken[0])})")
                conn.execute("ROLLBACK" if dry_run else "COMMIT")
                result["status"] = "rehearsed" if dry_run else "imported"
                if not dry_run:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            else:
                conn.execute("ROLLBACK")
                result["status"] = "already_imported"
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    finally:
        src.close()
    result["import_seconds"] = round(time.monotonic() - started, 1)
    if not dry_run:
        result["receipt"] = _Run(root, f"import-{entry_id}", now).receipt(result)
    return result
