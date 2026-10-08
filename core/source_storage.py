"""A transaction's sources (``Transaction.sources``): looking one up, writing a capture and its index and work,
and what a session already said.  The transaction keeps ``source()``, its prefetch and the read memo."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from ..contracts import (
    ContractError,
    SourceEvent,
    import_source_fingerprint,
    validate_capture,
)
from . import lexical_index
from .delete_storage import canonical, group_digest, purged_group_key
from .events import (
    indexed_terms,
    prepare_capture,
    query_terms,
    segment_key,
    stored_content_digest,
    withheld_tool_output,
)
from .inbox_rules import REKEY_MARKER, deleted_forms, deleted_text, holds_events, waiting
from .source_records import SourceWrite, StoredSource
from .visibility import allowed


def _seconds_apart(first: object, second: object) -> float:
    """How far apart two stored ISO times are; unreadable times are never close."""
    try:
        moments = [datetime.fromisoformat(str(value).replace("Z", "+00:00")) for value in (first, second)]
        return abs((moments[0] - moments[1]).total_seconds())
    except (TypeError, ValueError):
        return math.inf


class Sources:
    def __init__(self, transaction) -> None:
        self._tx = transaction

    def source_by_event_key(self, source_event_key: str, revision: int = 1) -> StoredSource | None:
        """Resolve a trusted host occurrence without bypassing source visibility."""
        if (
            type(source_event_key) is not str
            or not 1 <= len(source_event_key) <= 512
            or not source_event_key.strip()
            or "\x00" in source_event_key
        ):
            raise ContractError("INPUT_INVALID", "source_event_key")
        identity = canonical([self._tx.context.binding.installation_id, source_event_key])
        source = self._tx.source("event-" + hashlib.sha256(identity.encode("utf-8")).hexdigest(), revision)
        if source is not None:
            return source
        first_key = segment_key(source_event_key, 0)
        identity = canonical([self._tx.context.binding.installation_id, first_key])
        source = self._tx.source("event-" + hashlib.sha256(identity.encode("utf-8")).hexdigest(), revision)
        if source is not None and source.event.get("segment", {}).get("group_key") == source_event_key:
            return source
        return None

    def witnessed_at(self, source: StoredSource) -> str | None:
        """When a source was said or observed, as far as the store can tell.

        Its occurrence time, except for a capture an older adapter re-keyed: a
        restarted Hermes gateway numbers turns from 1 again, and that adapter
        copied the time of the earlier, unrelated message already stored under
        the reused key.  Such a copy is recognizable exactly -- the original's
        time on different content -- and the write time is the best one left.
        Stored rows are never rewritten; their fingerprints cover that time.
        """
        stamp = source.event.get("occurred_at")
        stamp = stamp if type(stamp) is str and stamp else None
        key = source.event.get("source_event_key")
        if stamp is None or type(key) is not str or REKEY_MARKER not in key:
            return stamp
        # The original is looked up by its stored key in the same scope, which
        # the unique (source_event_key, source_revision) index answers directly.
        conn = self._tx._check()
        original = conn.execute(
            "SELECT occurred_at,content_sha256 FROM source_events WHERE source_event_key=? AND source_revision=? AND scope_id=?",
            (key.split(REKEY_MARKER, 1)[0], source.revision, source.scope_id),
        ).fetchone()
        if original is None or original["occurred_at"] != stamp or original["content_sha256"] == source.content_sha256:
            return stamp
        row = conn.execute(
            "SELECT persisted_at FROM source_events WHERE event_id=? AND source_revision=?",
            (source.ref, source.revision),
        ).fetchone()
        return row["persisted_at"] if row is not None and row["persisted_at"] else stamp

    def source_current(self, ref: str) -> StoredSource | None:
        """Resolve the visible current source revision in one bounded lookup."""
        scopes = sorted(self._tx.context.allowed_scope_ids)
        if not scopes:
            return None
        marks = ",".join("?" for _ in scopes)
        row = (
            self._tx._check()
            .execute(
                f"""SELECT max(source_revision) AS revision FROM source_events
            WHERE event_id=? AND read_blocked=0 AND scope_id IN ({marks})
            AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)""",
                (ref, *scopes, self._tx.context.project_id, self._tx.context.branch_id),
            )
            .fetchone()
        )
        if row is None or row["revision"] is None:
            return None
        return self._tx.source(ref, int(row["revision"]))

    def _admitted_source(self, event: SourceEvent) -> dict:
        """Validate a capture against the contract, the import provenance and the
        admission policy; the stored event must be exactly the admitted one."""
        event = validate_capture(dict(event), self._tx.context)
        provenance = self._tx.context.import_provenance
        if provenance is not None:
            if (
                event.get("source_original_origin") != provenance.original_origin
                or import_source_fingerprint(event) not in provenance.source_fingerprints
            ):
                raise ContractError("ACCESS_DENIED", "import_provenance")
        admitted = prepare_capture(event, self._tx.context)
        if admitted.rejection or len(admitted.events) != 1 or admitted.events[0] != event:
            raise ContractError("INPUT_INVALID", "unprepared_source")
        return event

    def _same_identity(self, row, scope_id: str) -> bool:
        return (row["scope_id"], row["session_id"], row["project_id"], row["branch_id"]) == (
            scope_id,
            self._tx.context.session_id,
            self._tx.context.project_id,
            self._tx.context.branch_id,
        )

    def _check_source_group(self, conn, group_key: str, scope_id: str, revision: int, segment_total: int):
        """A segment group belongs to one identity and one segment count; a
        blocked group refuses new members.  Returns the group's block policy row."""
        digest = group_digest(
            self._tx.context.binding, scope_id, self._tx.context.project_id, self._tx.context.branch_id, group_key
        )
        policy = conn.execute(
            "SELECT read_blocked,suppressed FROM source_group_blocks WHERE group_sha256=?", (digest,)
        ).fetchone()
        if policy is not None and policy["read_blocked"]:
            raise ContractError("ACCESS_DENIED", "source_unavailable")
        for row in conn.execute(
            "SELECT scope_id,session_id,project_id,branch_id,source_revision,segment_total,read_blocked FROM source_events WHERE source_group_key=?",
            (group_key,),
        ):
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
        for row in conn.execute(
            "SELECT source_revision,event_sha256,scope_id,session_id,project_id,branch_id,read_blocked FROM source_events WHERE event_id=?",
            (ref,),
        ):
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
        condition literally present) is suppressed with it.

        The suppressed claims are picked first.  Subject and predicate are in the scope's index, so SQLite searched the
        content for those of every claim in the scope before it read whether one was suppressed: 8,995 claims, 11 of
        them suppressed, held the writer lease 1 s for a tool output of 51,283 characters.
        """
        return (
            conn.execute(
                """WITH muted AS MATERIALIZED (SELECT claim_id,subject,predicate,current_revision FROM claims
                WHERE scope_id=? AND project_id IS ? AND branch_id IS ? AND suppressed=1 AND read_blocked=0)
            SELECT 1 FROM muted c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
            WHERE v.state IN ('active','disputed') AND instr(?,c.subject)>0 AND instr(?,c.predicate)>0
            AND instr(?,json_extract(v.payload_json,'$.value_text'))>0
            AND NOT EXISTS(SELECT 1 FROM json_each(v.payload_json,'$.conditions') WHERE instr(?,value)=0) LIMIT 1""",
                (scope_id, self._tx.context.project_id, self._tx.context.branch_id, content, content, content, content),
            ).fetchone()
            is not None
        )

    def _copies_a_suppressed_source(self, conn, scope_id: str, group_key: str, event) -> bool:
        """A capture given a new key because another message held its key (``inbox_rules.REKEY_MARKER``) that is a
        copy of a suppressed or deleted message is suppressed with it.  Its new key is a group of its own, which the
        first message's suppression does not reach: a suppressed message sent again under a colliding key would come
        back to automatic recall.  A copy has the same role and words as a suppressed part in the same scope, project
        and branch (a digest outlasts a purge), or holds the words of the message whose key it took, compared as a
        delete compares them (``inbox_rules.holds``: whitespace aside, and so on).  A source group is suppressed
        whole: a part that is a copy suppresses the parts of its group stored before it and after it."""
        if REKEY_MARKER not in group_key:
            return False
        partition = (scope_id, self._tx.context.project_id, self._tx.context.branch_id)
        if conn.execute(
            """SELECT 1 FROM source_events WHERE source_group_key=? AND scope_id=? AND project_id IS ?
                           AND branch_id IS ? AND suppressed=1 LIMIT 1""",
            (group_key, *partition),
        ).fetchone():
            return True
        copy = (
            conn.execute(
                """SELECT 1 FROM source_events WHERE scope_id=? AND role=? AND content_sha256=? AND project_id IS ?
               AND branch_id IS ? AND suppressed=1 LIMIT 1""",
                (
                    scope_id,
                    event["role"],
                    hashlib.sha256(event["content"].encode("utf-8")).hexdigest(),
                    self._tx.context.project_id,
                    self._tx.context.branch_id,
                ),
            ).fetchone()
            is not None
        )
        if not copy:
            taken = conn.execute(
                """SELECT content FROM source_events WHERE source_group_key=? AND scope_id=? AND project_id IS ?
                   AND branch_id IS ? AND role=? AND suppressed=1 AND content<>''""",
                (group_key.split(REKEY_MARKER, 1)[0], *partition, event["role"]),
            ).fetchall()
            copy = bool(taken) and holds_events(
                [event],
                frozenset(),
                frozenset(),
                frozenset(deleted_text(row["content"]) for row in taken),
                rekeyed=True,
            )
        if copy:
            conn.execute(
                """UPDATE source_events SET suppressed=1 WHERE source_group_key=? AND scope_id=?
                            AND project_id IS ? AND branch_id IS ?""",
                (group_key, *partition),
            )
        return copy

    def _source_ref(self, key: str) -> str:
        return (
            "event-"
            + hashlib.sha256(canonical([self._tx.context.binding.installation_id, key]).encode("utf-8")).hexdigest()
        )

    def _hidden_refs(self, events) -> list[str]:
        """The refs a message's parts would take that a deletion hides."""
        return [
            ref
            for ref in (self._source_ref(event["source_event_key"]) for event in events)
            if not allowed(self._tx, "event", ref)
        ]

    @staticmethod
    def _deleted_words(rows) -> tuple[set, set]:
        """A deleted message's words: its text in each stored version whose parts all kept their words, and the forms
        of the words a purge kept."""
        versions: dict[int, list] = {}
        for row in rows:
            versions.setdefault(row["source_revision"], []).append((row, json.loads(row["extra_json"] or "{}")))
        texts, kept = set(), set()
        for parts in versions.values():
            if all(row["content"] for row, _extra in parts):
                texts.add(
                    deleted_text(
                        "".join(
                            row["content"] for row, _extra in sorted(parts, key=lambda part: part[0]["segment_index"])
                        )
                    )
                )
            elif all(row["purged"] for row, _extra in parts) and not any(
                "deleted_forms" in extra for _row, extra in parts
            ):
                # Purged by a release that kept no forms of the words: nothing tells a near copy there from another
                # message, so a message under that key is refused.  A deleted message with no text (attachments
                # alone) is not purged yet, and is compared by its digest.
                raise ContractError("ACCESS_DENIED", "source_unavailable")
            kept.update(form for _row, extra in parts for form in extra.get("deleted_forms") or ())
        return texts, kept

    def refuse_under_a_deleted_key(self, events, *, scope_id: str) -> None:
        """Refuse a message under a deleted message's key or source group, or tell another message from it.

        The deletion contract's least unit is a source group, with its later versions and missing parts: a revision
        the deleted group never stored, and a part sent without the message's first, are refused.  A whole message is
        compared with the deleted one, all its parts together (a first part changed by one character had let the
        second through, word for word): a part with a deleted part's digest; while the deleted words are kept, all of
        them held or a near copy, as a delete compares waiting captures (``inbox_rules.holds_events``); after the
        purge, the same words spaced, cased or punctuated otherwise (``inbox_rules.deleted_forms``).  A copy is
        refused (``source_unavailable``).  Anything else is a key collision (``VERSION_CONFLICT``), which the capture
        inbox stores under a key of its own: a restarted Hermes gateway numbers its turns from 1 again, and a delete
        removes its own command's key, so the next message at that turn would otherwise be refused.  After the
        purge, a copy with words added is not known by anything kept, and is stored as another message.  A key with
        nothing stored left to compare with (a restored absence) refuses whatever comes."""
        if not events:
            return
        conn = self._tx._check()
        first = events[0]
        segment = first.get("segment")
        group_key = segment["group_key"] if segment else first["source_event_key"]
        partition = (scope_id, self._tx.context.project_id, self._tx.context.branch_id)
        hidden = self._hidden_refs(events)
        block = conn.execute(
            "SELECT read_blocked FROM source_group_blocks WHERE group_sha256=?",
            (group_digest(self._tx.context.binding, *partition, group_key),),
        ).fetchone()
        if not hidden and not (block is not None and block["read_blocked"]):
            return
        # The deleted message's rows: under the refs this message's parts would take, under its key's own, and under its
        # group key, before the purge or as the purge left it.  A long message an older release purged had its group
        # key hashed once for each part and is under none of these: with nothing to compare, it refuses.
        refs = sorted({*hidden, self._source_ref(group_key)})
        rows = conn.execute(
            f"""SELECT source_revision,segment_index,content,content_sha256,extra_json,
                       source_event_key='removed-'||event_id AS purged FROM source_events
                WHERE read_blocked=1 AND (event_id IN ({",".join("?" for _ in refs)})
                   OR (source_group_key IN (?,?) AND scope_id=? AND project_id IS ? AND branch_id IS ?))""",
            (*refs, group_key, purged_group_key(group_key), *partition),
        ).fetchall()
        refuse = ContractError("ACCESS_DENIED", "source_unavailable")
        if not rows:
            raise refuse
        indexes = {event["segment"]["index"] for event in events if event.get("segment")}
        if first["source_revision"] not in {row["source_revision"] for row in rows} or (indexes and 0 not in indexes):
            raise refuse
        texts, kept = self._deleted_words(rows)
        ordered = sorted(events, key=lambda event: (event.get("segment") or {}).get("index", 0))
        if holds_events(
            events, frozenset(row["content_sha256"] for row in rows), frozenset(), frozenset(texts), rekeyed=True
        ) or kept & deleted_forms("".join(event["content"] for event in ordered)):
            raise refuse
        raise ContractError("VERSION_CONFLICT", "source_deleted_key")

    def put_source(
        self, event: SourceEvent, *, scope_id: str, persisted_at: str, capture_gaps: tuple[str, ...] = ()
    ) -> SourceWrite:
        conn = self._tx._check(write=True)
        self._tx._scope(scope_id)
        # Every source in a shared store names the entry it came in through.  One
        # arriving without is an adapter that forgot to say whose it is: refused,
        # not filed under the store's name.  A local store's rows are all ``local``.
        shared = self._tx.context.binding.installation_kind == "shared"
        if shared and self._tx.context.entry_id is None:
            raise ContractError("IDENTITY_UNBOUND", "entry_required")
        if not shared and self._tx.context.entry_id is not None:
            raise ContractError("IDENTITY_UNBOUND", "entry_unexpected")
        # The same statement records the entry's activity and proves it attached:
        # an entry the store never registered has no row to update.
        if (
            shared
            and conn.execute(
                "UPDATE entries SET last_seen=? WHERE entry_id=?", (persisted_at, self._tx.context.entry_id)
            ).rowcount
            != 1
        ):
            raise ContractError("IDENTITY_UNBOUND", "entry_unregistered")
        event = self._admitted_source(event)
        provenance = self._tx.context.import_provenance
        # First delivery's recorded_at is retained.  Transport retries may arrive
        # later; occurrence time and all provenance/content fields must agree.
        ref = self._source_ref(event["source_event_key"])

        # A message under a deleted key has been compared with the deleted one before its parts are stored
        # (``refuse_under_a_deleted_key``); a hidden key refuses whatever reaches it here.
        if not allowed(self._tx, "event", ref):
            raise ContractError("ACCESS_DENIED", "source_unavailable")
        revision = event["source_revision"]
        fingerprint_input = {k: v for k, v in event.items() if k != "recorded_at"}
        provenance_hash = provenance.manifest_sha256 if provenance else None
        fingerprint = hashlib.sha256(
            canonical(
                [
                    scope_id,
                    self._tx.context.session_id,
                    self._tx.context.project_id,
                    self._tx.context.branch_id,
                    fingerprint_input,
                    provenance_hash,
                ]
            ).encode("utf-8")
        ).hexdigest()
        segment = event.get("segment")
        group_key = segment["group_key"] if segment else event["source_event_key"]
        segment_index, segment_total = (segment["index"], segment["total"]) if segment else (0, 1)
        group_policy = self._check_source_group(conn, group_key, scope_id, revision, segment_total)
        if self._existing_revision(conn, ref, scope_id, revision, fingerprint):
            return SourceWrite("duplicate", ref, revision)
        columns = (
            "source_event_key",
            "origin",
            "role",
            "content",
            "occurred_at",
            "recorded_at",
            "time_precision",
            "capture_state",
        )
        extras = {
            k: v
            for k, v in event.items()
            if k not in {*columns, "protocol_version", "source_revision", "source_original_origin", "dataset_id"}
        }
        conn.execute(
            """INSERT INTO source_events(event_id,source_revision,scope_id,session_id,project_id,branch_id,
            source_event_key,origin,role,content,occurred_at,recorded_at,time_precision,capture_state,
            content_sha256,event_sha256,persisted_at,source_original_origin,dataset_id,extra_json,
            source_group_key,segment_index,segment_total,capture_gaps_json,import_provenance_sha256,entry_id,source_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,(SELECT COALESCE(MAX(source_id),0)+1 FROM source_events))""",
            (
                ref,
                revision,
                scope_id,
                self._tx.context.session_id,
                self._tx.context.project_id,
                self._tx.context.branch_id,
                *(event[k] for k in columns),
                hashlib.sha256(event["content"].encode("utf-8")).hexdigest(),
                fingerprint,
                persisted_at,
                event.get("source_original_origin"),
                event.get("dataset_id"),
                canonical(extras),
                group_key,
                segment_index,
                segment_total,
                canonical(capture_gaps),
                provenance_hash,
                self._tx.context.entry_id or "local",
            ),
        )
        if (
            (group_policy is not None and group_policy["suppressed"])
            or self._inherits_suppression(conn, scope_id, event["content"])
            or self._copies_a_suppressed_source(conn, scope_id, group_key, event)
        ):
            conn.execute(
                "UPDATE source_events SET suppressed=1 WHERE event_id=? AND source_revision=?", (ref, revision)
            )
        conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        return SourceWrite("inserted", ref, revision)

    def index_source(self, ref: str, revision: int) -> None:
        conn = self._tx._check(write=True)
        source = self._tx.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        identity = lexical_index.source_id(conn, ref, revision)
        terms = indexed_terms(source.event)
        if withheld_tool_output(source.event):
            # A withheld output's placeholder is indexed by its error text alone; what an older release gave it
            # beyond that goes.
            lexical_index.unindex_beyond(conn, identity, terms)
        lexical_index.index_terms(conn, identity, terms)

    def source_projection_status(self, ref: str, revision: int) -> tuple[str, str]:
        conn = self._tx._check()
        source = self._tx.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        actual = lexical_index.terms_of(conn, lexical_index.source_id(conn, ref, revision))
        lexical = "ready" if actual == indexed_terms(source.event) else "not_ready"
        work = conn.execute(
            "SELECT state FROM work_items WHERE work_type='embed' AND subject_ref=? AND subject_revision=?",
            (ref, revision),
        ).fetchone()
        semantic = (
            "not_scheduled"
            if work is None
            else {
                "pending": "pending",
                "leased": "pending",
                "done": "ready",
                "failed": "failed",
                "obsolete": "obsolete",
            }[work[0]]
        )
        return lexical, semantic

    def source_authorization(self, ref: str, revision: int) -> dict | None:
        """The scope authorization a migrated source was admitted under, or ``None``."""
        row = (
            self._tx._check()
            .execute(
                """SELECT p.payload FROM source_authorizations a JOIN authorization_payloads p ON p.authorization_id=a.authorization_id
               WHERE a.event_id=? AND a.source_revision=?""",
                (ref, revision),
            )
            .fetchone()
        )
        return None if row is None else json.loads(row[0])

    def search_sources(
        self, query: str, *, limit: int = 20, history: bool = False, automatic: bool = False
    ) -> tuple[StoredSource, ...]:
        conn = self._tx._check()
        if type(limit) is not int or not 1 <= limit <= 200 or type(history) is not bool or type(automatic) is not bool:
            raise ContractError("INPUT_INVALID", "search_limit")
        terms = query_terms(query)
        if not terms:
            return ()
        scopes = sorted(self._tx.context.allowed_scope_ids)
        term_marks = ",".join("?" for _ in terms)
        scope_marks = ",".join("?" for _ in scopes)
        current = (
            ""
            if history
            else "AND NOT EXISTS (SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)"
        )
        suppression = (
            "AND e.suppressed=0 AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event' AND b.object_ref=e.event_id AND b.suppressed=1)"
            if automatic
            else ""
        )
        # ``+`` keeps the scope filter from choosing an index: the statement starts from the terms however many
        # there are (as the lexical channel's does, retrieval_storage.lexical).
        rows = conn.execute(
            f"""SELECT e.event_id,e.source_revision,count(*) AS hits FROM {lexical_index.JOIN}
            WHERE t.term IN ({term_marks}) AND +e.scope_id IN ({scope_marks}) AND e.read_blocked=0
            AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)
            AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event' AND b.object_ref=e.event_id AND b.read_blocked=1)
            {current} {suppression}
            GROUP BY e.event_id,e.source_revision
            ORDER BY hits DESC,e.occurred_at DESC,e.event_id,e.source_revision DESC LIMIT ?""",
            (*terms, *scopes, self._tx.context.project_id, self._tx.context.branch_id, limit),
        ).fetchall()
        return tuple(
            source for row in rows if (source := self._tx.source(row["event_id"], row["source_revision"])) is not None
        )

    def said_in_session(
        self, scope_id: str, items: tuple[tuple[str, str, str, str | None], ...], *, window_seconds: float
    ) -> tuple[bool, ...]:
        """For each (role, content, occurred_at, host_key): whether this session already holds that message.

        A host that records one message by two routes -- a hook as it happens, its session record later --
        asks this before the second.  A message the host names is held only under ``host_key``, the key its
        hook wrote, so the same short words said again are a new message.  One it does not name is held by
        its words said within ``window_seconds`` of that time.  Each copy answers for one message, and a
        hook's capture still waiting in the inbox counts as held: the inbox stores it later.
        """
        self._tx._scope(scope_id)
        conn = self._tx._check()
        waiting = self._waiting_in_inbox(scope_id)
        waiting_keys = {key for found in waiting.values() for _stamp, key in found}
        answers = [False] * len(items)
        # Named messages first, so the same words said again cannot take the copy a named message owns.
        named = {host_key for *_said, host_key in items if host_key is not None}
        # A message over 65,536 characters is stored in segments under keys of their own, grouped under the host's
        # key: looked up by the host's key alone, it was never found, and the Stop's read of the session record
        # stored a long prompt a second time.  A message stored whole is its own group.
        # A named message that was deleted counts as said as well: once the delete is purged its rows no longer
        # carry the key, and a record read would store the words again under a key of the record's.

        for index, (_role, _content, _occurred_at, host_key) in enumerate(items):
            if host_key is not None:
                answers[index] = (
                    host_key in waiting_keys
                    or conn.execute(
                        "SELECT 1 FROM source_events WHERE source_group_key=? AND scope_id=? AND session_id=? LIMIT 1",
                        (host_key, scope_id, self._tx.context.session_id),
                    ).fetchone()
                    is not None
                    or conn.execute(
                        "SELECT 1 FROM source_group_blocks WHERE group_sha256=? AND read_blocked=1",
                        (
                            group_digest(
                                self._tx.context.binding,
                                scope_id,
                                self._tx.context.project_id,
                                self._tx.context.branch_id,
                                host_key,
                            ),
                        ),
                    ).fetchone()
                    is not None
                )
        copies: dict[tuple[str, str], list[tuple[object, str]]] = {}
        for index, (role, content, occurred_at, host_key) in enumerate(items):
            if host_key is not None:
                continue
            digest = stored_content_digest(content)
            if (role, digest) not in copies:
                copies[role, digest] = [
                    (stamp, key)
                    for stamp, key in (
                        *conn.execute(
                            "SELECT occurred_at,source_group_key FROM source_events "
                            "WHERE scope_id=? AND role=? AND content_sha256=? AND session_id=?",
                            (scope_id, role, digest, self._tx.context.session_id),
                        ).fetchall(),
                        *waiting.get((role, digest), ()),
                    )
                    if key not in named
                ]
            found = copies[role, digest]
            near = [
                (distance, position)
                for position, (stamp, _key) in enumerate(found)
                if (distance := _seconds_apart(stamp, occurred_at)) <= window_seconds
            ]
            if near:
                found.pop(min(near)[1])
            answers[index] = bool(near)
        return tuple(answers)

    def _waiting_in_inbox(self, scope_id: str) -> dict[tuple[str, str], list[tuple[object, str]]]:
        """This session's captures a replay of the inbox will still store, by (role, content digest)."""
        still_waiting: dict[tuple[str, str], list[tuple[object, str]]] = {}
        for payload, code in self._tx._check().execute(
            "SELECT payload_json,last_error_code FROM capture_inbox WHERE scope_id=? AND project_id IS ? AND branch_id IS ?",
            (scope_id, self._tx.context.project_id, self._tx.context.branch_id),
        ):
            if not waiting(code):
                continue
            try:
                body = json.loads(payload)
            except ValueError:
                continue
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("context"), dict)
                or body["context"].get("session_id") != self._tx.context.session_id
            ):
                continue
            for event in body.get("events") or ():
                if isinstance(event, dict) and type(event.get("content")) is str and type(event.get("role")) is str:
                    # A segment answers to its message's key, as its stored rows do (``said_in_session``).
                    segment = event.get("segment")
                    key = segment.get("group_key") if isinstance(segment, dict) else event.get("source_event_key")
                    still_waiting.setdefault((event["role"], stored_content_digest(event["content"])), []).append(
                        (event.get("occurred_at"), str(key))
                    )
        return still_waiting

    def enqueue_source(self, ref: str, revision: int, *, work_type: str, available_at: str) -> None:
        conn = self._tx._check(write=True)
        if work_type not in {"consolidate", "embed"}:
            raise ContractError("INPUT_INVALID", "work_type")
        source = self._tx.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        conn.execute(
            """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(work_type,subject_ref,subject_revision) DO NOTHING""",
            (work_type, ref, revision, source.scope_id, source.project_id, source.branch_id, available_at),
        )
