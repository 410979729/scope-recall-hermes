"""Read-only SQLite collection, hydration, and collection pagination.

This module is intentionally coupled to the existing ``Transaction`` surface,
which remains the only authority for source visibility and versioned objects.
No function here writes, schedules work, increments counters, or releases text
outside the trusted context.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
from typing import Iterable, cast

from ..contracts import SOURCE_CONTEXTS_MAX_ITEMS, ContractError, SourceContext, bounded_source_context
from .claim_storage import parse_source_ref
from .claims import canonical_time, select_effective, select_proposal
from .delete_storage import canonical
from .episodes import source_origin
from .events import lexical_terms
from .recall_policy import (
    applicability,
    claim_embedding_text,
    hard_identifiers,
    in_time_window,
    meaningful_query_terms,
    parse_time,
    query_is_relevant,
    synonym_expansions,
)
from .resume_compaction import resume_evidence_refs
from .retrieval import STALE_RESUME_GAPS, CandidateRef, CollectionQuery, ObjectKind, PageCursor, RetrievedObject, SearchContext
from .visibility import CLOSED_INTENTION_STATES, OBJECT_KINDS, allowed

#: Modes in which a delivered source must still be live, not merely visible.
LIVE_MODES = frozenset({"auto", "current", "method"})
# An episode's lineage rows sit at the revision each source or artifact entered
# (Episodes.attach); the episode they name is the one at its head.
_HEAD_REVISION = ("CASE WHEN object_kind='episode' THEN COALESCE((SELECT current_revision FROM episodes "
                  "WHERE episode_id=object_ref),object_revision) ELSE object_revision END")
_EXACT_REF = re.compile(r"(?:event|claim|episode|artifact|reference)-[A-Za-z0-9._/-]+@\d+")
_HEAD_TABLES = {
    "episode": ("episodes", "episode_id"),
    "artifact": ("artifacts", "artifact_id"),
    "reference": ("reference_bindings", "reference_id"),
}
_RETAINED_ARTIFACT_STATES = frozenset({"retained_artifact", "described_artifact", "reference_only"})
#: Origins that are a first-hand record rather than an echo of the system's own
#: output. Matches the set already used by consolidation result validation.
_FIRST_HAND_ORIGINS = frozenset({"human_direct", "tool_observation"})
#: A term in at least this share of all sources tells the ranker nothing: it
#: matches most of the corpus, so it separates nothing while costing the longest
#: posting list in the index.  Measured, the terms that clear this bar are JSON
#: field names from tool-observation envelopes, not anything a person wrote.
_LEXICAL_DF_FRACTION = 0.10
#: Floor so a young or small instance is never pruned: on a corpus of thirty
#: sources, "10% of everything" is three, and ordinary words would vanish.
_LEXICAL_DF_FLOOR = 64


def scope_digest(context) -> str:
    payload = [
        context.binding.agent_id,
        context.binding.installation_id,
        sorted(context.allowed_scope_ids),
        context.project_id,
        context.branch_id,
    ]
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _marks(values: Iterable[object]) -> str:
    values = tuple(values)
    if not values:
        raise ContractError("ACCESS_DENIED", "scope")
    return ",".join("?" for _ in values)


def _source_key(ref: str, revision: int) -> str:
    return f"{ref}@{revision}"


def _is_source_ref(value: object) -> bool:
    try:
        parse_source_ref(cast(str, value))
    except ContractError:
        return False
    return True


def _source_live(tx, ref: str, revision: int) -> bool:
    try:
        tx.claims.require_live_source(ref, revision)
    except ContractError:
        return False
    return True


def _has_first_hand_root(tx, evidence: Iterable[str]) -> bool:
    """Whether any evidence root is first-hand rather than the system's own echo.

    Admitting unpromoted proposals to recall must not admit claims derived only
    from assistant output or re-injected memory: that closes a loop in which the
    assistant's own words come back as remembered facts.  A promoted claim has
    already passed qualification, so this applies only to proposals.  Uses
    ``source_origin`` so an imported record is judged by its verified original
    lineage, not by the fact that it arrived through import.
    """
    for ref in evidence:
        try:
            source_ref, source_revision = parse_source_ref(ref)
        except ContractError:
            continue
        source = tx.source(source_ref, source_revision)
        if source is not None and source_origin(source) in _FIRST_HAND_ORIGINS:
            return True
    return False


def _discriminating_terms(tx, terms: tuple[str, ...], keep: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Drop query terms too common to separate anything.

    Document frequency is read once for the query's own terms, which is a
    clustered range scan: ``lexical_projection`` is WITHOUT ROWID keyed on
    ``(term, event_id, source_revision)``.  If every term is that common the
    query keeps its rarest ones: answering from a weak signal beats answering
    from none, and the vector and recent channels still contribute.

    ``keep`` is never dropped however common: the query's hard identifiers,
    which hydration requires of every source.  Without them the SQL cannot
    reach a single source hydration would admit.
    """
    conn = tx._check()
    frequencies = {
        row[0]: int(row[1])
        for row in conn.execute(
            f"SELECT term,COUNT(*) FROM lexical_projection WHERE term IN ({_marks(terms)}) GROUP BY term",
            terms,
        ).fetchall()
    }
    if not frequencies:
        return terms
    ceiling = _common_term_ceiling(conn)
    kept = tuple(term for term in terms if term in keep or frequencies.get(term, 0) < ceiling)
    if kept:
        return kept
    rarest = min(frequencies.values())
    return tuple(term for term in terms if frequencies.get(term, 0) == rarest) or terms


def _common_term_ceiling(conn) -> int:
    """Document frequency at which a term is too common to separate anything."""
    corpus = int(conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0] or 0)
    return max(_LEXICAL_DF_FLOOR, int(corpus * _LEXICAL_DF_FRACTION))


def _discriminating_synonyms(tx, synonyms: dict[str, str], terms: tuple[str, ...]) -> dict[str, str]:
    """The synonym terms worth searching, each still mapped to the query term it stands in for.

    A synonym term goes when the query term it stands in for was pruned, and it
    is held to the same document-frequency bar without the query terms'
    fallback: one too common to separate anything is not searched, and neither
    is one the index has never seen, which could not match.
    """
    live = {term: original for term, original in synonyms.items() if original in terms}
    if not live:
        return {}
    conn = tx._check()
    frequencies = {
        row[0]: int(row[1])
        for row in conn.execute(
            f"SELECT term,COUNT(*) FROM lexical_projection WHERE term IN ({_marks(live)}) GROUP BY term",
            tuple(live),
        ).fetchall()
    }
    if not frequencies:
        return {}
    ceiling = _common_term_ceiling(conn)
    return {term: original for term, original in live.items() if 0 < frequencies.get(term, 0) < ceiling}


def evidence_source_contexts(tx, evidence: Iterable[str]) -> list[SourceContext]:
    """Distinct, bounded source contexts of the sources behind ``evidence`` refs."""
    collected: list[SourceContext] = []
    for ref in evidence:
        try:
            source_ref, source_revision = parse_source_ref(ref)
        except ContractError:
            continue
        source = tx.source(source_ref, source_revision)
        if source is None:
            continue
        context = bounded_source_context(source.event.get("source_context"))
        if context is None or context in collected:
            continue
        collected.append(context)
        if len(collected) >= SOURCE_CONTEXTS_MAX_ITEMS:
            break
    return collected


def _source_contexts_metadata(contexts: list[SourceContext]) -> tuple[tuple[str, str], ...]:
    if not contexts:
        return ()
    return (("source_contexts", json.dumps(contexts, ensure_ascii=False, separators=(",", ":"))),)


#: Claims one query scores after the SQL prefilter.  Scoring loads a claim's
#: versions, so this bounds a query's cost on an instance with many claims.
_CLAIM_SCAN_LIMIT = 256
#: Query terms the prefilter looks for, longest first (a query may have 128).
_CLAIM_PREFILTER_TERMS = 32


def _claim_version_for(versions, context: SearchContext, instant: str):
    """The version of one claim that answers in this mode, as hydration would admit it."""
    if not versions:
        return None
    if context.mode == "as_of":
        return select_effective(versions, instant, as_of=True)
    effective = select_effective(versions, instant)
    if effective is not None:
        return effective
    if context.mode == "history":
        return max(versions, key=lambda version: version.revision)
    return select_proposal(versions, instant)


def _claim_answers(hits: int, covered: float, term_count: int, *, proposed: bool) -> bool:
    """Whether a claim's statement answers the query well enough to be offered.

    The query must name the claim's subject -- at least half of its terms -- and
    hit the statement beyond a single word; a one-term query can only name a
    subject outright.  An unpromoted proposal needs one hit more: it is offered
    as a lead, and a lead that shares two words is noise.
    """
    if covered < 0.5:
        return False
    needed = 1 if term_count == 1 else 2
    return hits >= needed + (1 if proposed else 0)


#: How far back a reply's turn is looked for.  A turn that ran longer than this
#: is judged from its last two hours only.
_TURN_LOOKBACK = timedelta(hours=2)
#: Memory lookups in one turn that make its reply a restatement.  On one instance and
#: another instance the replies that tested recall ran 3 to 15 of them; replies that used
#: memory to answer a question mostly ran one or two, and those stay evidence.
RECALL_ECHO_MIN_LOOKUPS = 3
#: What a lookup that returned memory looks like: recall items, entity
#: statements, profile sections.  A status lookup returns none of them.
_MEMORY_RESULT = ("instr(content,'\"items\":')>0 OR instr(content,'\"statements\":')>0"
                  " OR instr(content,'\"sections\":')>0")


def _second_precision(moment) -> str:
    """A bound that compares correctly with stored ISO times, whatever their fraction and zone suffix."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def recall_echo(tx, source) -> bool:
    """Whether an assistant reply restates what memory lookups in its turn returned.

    another instance tested her own recall and reported the queries and what came back;
    the report then came back first for those very queries, above the evidence
    it quoted.  The turn is the stretch since the session's last user message,
    which the host captures before any tool runs, and the reply is dated as the
    store can best tell (``witnessed_at``), since that report's own row carried
    a day-old time.
    """
    if source.event.get("origin") != "assistant_visible":
        return False
    stamp = tx.witnessed_at(source)
    if stamp is None:
        return False
    try:
        at = parse_time(stamp)
    except ContractError:
        return False
    conn = tx._check()
    upper = _second_precision(at + timedelta(seconds=1))
    lower = _second_precision(at - _TURN_LOOKBACK)
    started = conn.execute(
        """SELECT max(occurred_at) FROM source_events WHERE scope_id=? AND occurred_at>=? AND occurred_at<?
           AND session_id=? AND role='user' AND origin!='memory_reinjection'""",
        (source.scope_id, lower, upper, source.session_id)).fetchone()[0]
    lookups = conn.execute(
        f"""SELECT count(*) FROM (SELECT 1 FROM source_events WHERE scope_id=? AND occurred_at>=? AND occurred_at<?
            AND session_id=? AND origin='memory_reinjection' AND ({_MEMORY_RESULT}) LIMIT ?)""",
        (source.scope_id, started or lower, upper, source.session_id, RECALL_ECHO_MIN_LOOKUPS)).fetchone()[0]
    return lookups >= RECALL_ECHO_MIN_LOOKUPS


def _occurred_metadata(stamp: str | None) -> tuple[tuple[str, str], ...]:
    return (("occurred_at", stamp),) if stamp else ()


def _newest(stamps: Iterable[str | None]) -> str | None:
    """The latest of several ISO-8601 times; unparseable ones are skipped."""
    newest: tuple[object, str] | None = None
    for stamp in stamps:
        if not stamp:
            continue
        try:
            parsed = parse_time(stamp)
        except ContractError:
            continue
        if newest is None or parsed > newest[0]:
            newest = (parsed, stamp)
    return newest[1] if newest else None


def _claim_content(version) -> str:
    """Keep the complete qualified payload available to the packet compiler."""
    return json.dumps(version.payload, ensure_ascii=False, sort_keys=True)


def _claim_statement_content(version) -> str:
    """What a reader is shown for a claim: its payload without the quoted spans.

    The spans are verbatim evidence, and a tool-derived claim quotes escaped
    JSON: two such claims used most of a 4096-unit packet on one instance and pushed
    the answering messages out.  The item's evidence_refs already name every
    source, the full payload stays in metadata, and nothing on the read path
    matches against the quotes.
    """
    return json.dumps({key: value for key, value in version.payload.items() if key != "evidence_spans"},
                      ensure_ascii=False, sort_keys=True)


def _claim_status(version, current_effective: bool) -> str:
    if version.state == "proposed":
        return "historical"
    if version.state == "disputed":
        return "disputed"
    if current_effective or version.revision == version.current_revision:
        return "current"
    return "historical"


def _versioned_body(kind: str, obj) -> tuple[str, str, bool]:
    """Content, basis/origin, and whether the object's status is unknowable."""
    if kind == "episode":
        return json.dumps(obj.resume or {"state": obj.state}, ensure_ascii=False, sort_keys=True), "derived_summary", False
    if kind == "artifact":
        return obj.label, "observed", obj.retention_state not in _RETAINED_ARTIFACT_STATES
    payload = json.dumps(obj.payload, ensure_ascii=False, sort_keys=True)
    return payload, "derived_summary", obj.payload.get("resolution") in {"ambiguous", "unresolved"}


@dataclass(frozen=True)
class CollectionPage:
    items: tuple[RetrievedObject, ...]
    next_cursor: PageCursor | None
    coverage: str
    memory_epoch: int


#: How long after a person's message its turn's replies may come, and how many are followed.
TURN_REPLY_SECONDS = 1800
TURN_REPLY_LIMIT = 3


class RetrievalStorage:
    """Typed read boundary used by one ``RetrievalPipeline`` instance."""

    def __init__(self, *, clock=None):
        self.clock = clock if clock is not None else time

    def _remaining(self, context: SearchContext) -> float:
        return context.deadline - self.clock.monotonic()

    def epoch(self, tx) -> int:
        return tx.memory_epoch()

    # -- candidate channels ---------------------------------------------------

    def lexical(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        terms = meaningful_query_terms(context.query)
        if not terms:
            return ()
        # Hydration admits only content naming one of the query's hard
        # identifiers (``identifiers_compatible``).  A term naming one is never
        # pruned as common, and rows holding one rank first: otherwise sources
        # sharing more generic terms fill the pool and the admissible source is
        # never hydrated.
        requested = hard_identifiers(context.query)
        identifiers = tuple(term for term in terms if requested.intersection(hard_identifiers(term)))
        terms = _discriminating_terms(tx, terms, keep=identifiers)
        # A synonym term matches as the query term it stands in for, so hits
        # and matched terms still count the query's own terms, once each.
        # Without a synonym the statement and its parameters are unchanged.
        synonyms = _discriminating_synonyms(tx, synonym_expansions(context.query), terms)
        credit = f"CASE p.term {' '.join('WHEN ? THEN ?' for _ in synonyms)} ELSE p.term END" if synonyms else "p.term"
        credits = tuple(value for pair in synonyms.items() for value in pair)
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        term_marks, scope_marks = _marks((*terms, *synonyms)), _marks(scopes)
        identified = f"MAX(p.term IN ({_marks(identifiers)})) DESC," if identifiers else ""
        current = "" if context.mode in {"history", "as_of"} else "AND NOT EXISTS (SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)"
        as_of = ""
        params: list[object] = [*terms, *synonyms, *scopes, context.trusted_context.project_id, context.trusted_context.branch_id]
        if context.as_of is not None:
            as_of = " AND (e.occurred_at IS NULL OR e.occurred_at<=?)"
            params.append(context.as_of)
        rows = tx._check().execute(
            f"""SELECT e.event_id,e.source_revision,COUNT(DISTINCT {credit}) AS hits,
                       GROUP_CONCAT(DISTINCT hex({credit})) AS matched_term_hexes
                FROM lexical_projection p JOIN source_events e
                ON e.event_id=p.event_id AND e.source_revision=p.source_revision
                WHERE p.term IN ({term_marks}) AND e.scope_id IN ({scope_marks})
                  AND e.read_blocked=0 AND (e.project_id IS NULL OR e.project_id=?)
                  AND (e.branch_id IS NULL OR e.branch_id=?)
                  AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                      AND b.object_ref=e.event_id AND b.read_blocked=1)
                  {current}{as_of}
                GROUP BY e.event_id,e.source_revision
                ORDER BY CASE
                    WHEN e.role='tool' AND (
                        e.origin='memory_reinjection'
                        OR (e.origin='imported' AND e.source_original_origin='memory_reinjection')
                    ) THEN 1 ELSE 0
                END,
                {identified}hits DESC,e.occurred_at DESC,e.event_id,e.source_revision DESC
                LIMIT ?""",
            (*credits, *credits, *params, *identifiers, limit),
        ).fetchall()
        return tuple(
            CandidateRef(
                "event",
                row["event_id"],
                row["source_revision"],
                "lexical",
                rank=index,
                lexical_score=float(row["hits"]),
                matched_query_terms=tuple(sorted(
                    bytes.fromhex(encoded).decode("utf-8")
                    for encoded in row["matched_term_hexes"].split(",")
                )),
            )
            for index, row in enumerate(rows, 1)
        )

    def claims(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        """Claims whose own statement answers the query.

        The lexical, vector and recent channels all yield events, so a fact used
        to reach recall only through relation expansion out of an event that was
        retrieved first.  On one instance's benchmark 19 of the 29 facts recall missed
        were never reached at all: newer talk about the same subject filled the
        lexical pool before the fact's own evidence.  A claim is short and
        structured, so it is matched on its statement, with its own rule
        (``_claim_answers``) instead of the event specificity bar.

        One candidate per claim: the version answering in this mode, chosen with
        the same selectors hydration applies, so a replaced value is never the
        one offered.
        """
        terms = frozenset(meaningful_query_terms(context.query))
        if not terms or limit <= 0:
            return ()
        trusted = context.trusted_context
        scopes = tuple(sorted(trusted.allowed_scope_ids))
        needles = tuple(sorted(terms, key=lambda term: (-len(term), term))[:_CLAIM_PREFILTER_TERMS])
        matched = " + ".join("(instr(statement,?)>0)" for _ in needles)
        rows = tx._check().execute(
            f"""WITH heads AS MATERIALIZED (
                    SELECT c.claim_id AS claim_id,
                           lower(c.subject || ' ' || c.predicate || ' ' ||
                                 coalesce(json_extract(v.payload_json,'$.value_text'),'')) AS statement
                    FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
                    WHERE c.scope_id IN ({_marks(scopes)}) AND c.read_blocked=0 AND c.kind!='alias'
                      AND (c.project_id IS NULL OR c.project_id=?) AND (c.branch_id IS NULL OR c.branch_id=?)
                      AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='claim'
                          AND b.object_ref=c.claim_id AND b.read_blocked=1))
                SELECT claim_id FROM (SELECT claim_id, {matched} AS hits FROM heads)
                WHERE hits>0 ORDER BY hits DESC, claim_id LIMIT ?""",
            (*scopes, trusted.project_id, trusted.branch_id, *needles, _CLAIM_SCAN_LIMIT),
        ).fetchall()
        instant = context.as_of or context.now
        scored = []
        for row in rows:
            chosen = _claim_version_for(tx.claims.versions(row["claim_id"]), context, instant)
            if chosen is None or chosen.payload.get("kind") == "alias":
                continue
            try:
                statement = claim_embedding_text(chosen.payload)
            except ContractError:
                continue
            hits = terms.intersection(lexical_terms(statement))
            subject = {term for term in lexical_terms(str(chosen.payload.get("subject") or "")) if len(term) > 1}
            covered = len(subject & terms) / len(subject) if subject else 0.0
            proposed = chosen.state == "proposed"
            if not _claim_answers(len(hits), covered, len(terms), proposed=proposed):
                continue
            rank_key = (not proposed, len(hits) + covered, canonical_time(chosen.recorded_from) or "", chosen.ref)
            scored.append((rank_key, chosen, hits))
        scored.sort(key=lambda entry: entry[0], reverse=True)
        return tuple(
            CandidateRef("claim", chosen.ref, chosen.revision, "claim_lexical", rank=index,
                         lexical_score=float(len(hits)), matched_query_terms=tuple(sorted(hits)))
            for index, (_key, chosen, hits) in enumerate(scored[:limit], 1)
        )

    def exact(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        seen: set[tuple[str, str, int]] = set()
        candidates: list[CandidateRef] = []
        for raw in (*context.focus_refs, *_EXACT_REF.findall(context.query)):
            try:
                identity, version = raw.rsplit("@", 1)
                revision = int(version)
            except (ValueError, AttributeError):
                continue
            kind = next((name for name in OBJECT_KINDS if identity.startswith(name + "-")), None)
            if kind is None or revision < 1 or (kind, identity, revision) in seen:
                continue
            seen.add((kind, identity, revision))
            candidates.append(CandidateRef(cast(ObjectKind, kind), identity, revision, "exact_ref", rank=len(candidates) + 1))
            if len(candidates) >= limit:
                break
        return tuple(candidates)

    def recent(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        trusted = context.trusted_context
        scopes = tuple(sorted(trusted.allowed_scope_ids))
        excluded = set(context.current_source_refs)
        rows = tx._check().execute(
            f"""SELECT e.event_id,e.source_revision,e.content,e.recorded_at
                FROM source_events e JOIN work_items w
                ON w.subject_ref=e.event_id AND w.subject_revision=e.source_revision
                WHERE w.work_type='consolidate' AND w.state IN ('pending','leased')
                  AND e.session_id=? AND e.scope_id IN ({_marks(scopes)})
                  AND (e.project_id IS NULL OR e.project_id=?)
                  AND (e.branch_id IS NULL OR e.branch_id=?)
                  AND e.read_blocked=0 AND e.suppressed=0
                  AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                      AND b.object_ref=e.event_id AND (b.read_blocked=1 OR b.suppressed=1))
                ORDER BY e.recorded_at DESC,e.event_id,e.source_revision DESC LIMIT ?""",
            (trusted.session_id, *scopes, trusted.project_id, trusted.branch_id, limit * 4),
        ).fetchall()
        result = []
        for row in rows:
            key = _source_key(row["event_id"], row["source_revision"])
            if key in excluded or not query_is_relevant(context.query, row["content"]):
                continue
            result.append(CandidateRef("event", row["event_id"], row["source_revision"], "recent_raw", rank=len(result) + 1, lexical_score=1.0))
            if len(result) >= limit:
                break
        return tuple(result)

    def turn_replies(self, tx, candidate: CandidateRef) -> tuple[CandidateRef, ...]:
        """What the assistant said back in the turn a person's message opened.

        Episode membership reaches a reply only through every event of its
        episode, in id order, so a recalled question used up the relation bound
        long before its own answer.  A turn's replies are the assistant's
        visible messages in the same scope and session after the message, in
        capture order, until the person speaks again.  A gateway can capture a
        whole turn under one timestamp, so equal times fall back to rowid.
        """
        if candidate.kind != "event":
            return ()
        conn = tx._check()
        row = conn.execute(
            """SELECT rowid,scope_id,session_id,role,origin,occurred_at FROM source_events
               WHERE event_id=? AND source_revision=?""",
            (candidate.ref, candidate.revision),
        ).fetchone()
        if row is None or row["role"] != "user" or row["origin"] != "human_direct" or not row["occurred_at"]:
            return ()
        opened = canonical_time(row["occurred_at"])
        if opened is None:
            return ()
        window_end = (datetime.fromisoformat(opened) + timedelta(seconds=TURN_REPLY_SECONDS)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        rows = conn.execute(
            """SELECT event_id,source_revision,role,origin FROM source_events
               WHERE scope_id=? AND occurred_at>=? AND occurred_at<=? AND session_id=?
                 AND (occurred_at>? OR rowid>?) AND read_blocked=0 AND suppressed=0
               ORDER BY occurred_at,rowid LIMIT 64""",
            (row["scope_id"], row["occurred_at"], window_end, row["session_id"], row["occurred_at"], row["rowid"]),
        ).fetchall()
        replies: list[CandidateRef] = []
        for reply in rows:
            if reply["role"] == "user":
                break
            if reply["role"] == "assistant" and reply["origin"] == "assistant_visible":
                replies.append(CandidateRef("event", reply["event_id"], int(reply["source_revision"]), "relation",
                                            rank=len(replies) + 1, lexical_score=1.0))
                if len(replies) >= TURN_REPLY_LIMIT:
                    break
        return tuple(replies)

    def related(self, tx, candidate: CandidateRef, *, limit: int) -> tuple[CandidateRef, ...]:
        rows = tx._check().execute(
            f"""SELECT object_kind,object_ref,{_HEAD_REVISION} AS object_revision FROM evidence_links
               WHERE source_ref=? UNION SELECT 'event',source_ref,source_revision
               FROM evidence_links WHERE object_kind=? AND object_ref=?
               UNION SELECT dependency_kind,dependency_ref,dependency_revision
               FROM object_dependencies WHERE object_kind=? AND object_ref=?
               UNION SELECT object_kind,object_ref,{_HEAD_REVISION}
               FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?
               ORDER BY object_kind,object_ref,object_revision LIMIT ?""",
            (candidate.ref, candidate.kind, candidate.ref, candidate.kind, candidate.ref, candidate.kind, candidate.ref, limit),
        ).fetchall()
        result = []
        seen: set[tuple[str, str, int]] = set()
        for row in rows:
            key = (row["object_kind"], row["object_ref"], row["object_revision"])
            if key[0] not in OBJECT_KINDS or key in seen or key == candidate.key:
                continue
            seen.add(key)
            result.append(CandidateRef(key[0], key[1], key[2], "relation", rank=len(result) + 1, lexical_score=1.0))
            if len(result) >= limit:
                break
        return tuple(result)

    # -- hydration ------------------------------------------------------------

    def _evidence(self, tx, kind: str, ref: str, revision: int, context: SearchContext) -> tuple[str, ...] | None:
        """The object's evidence refs, or ``None`` when any of them is not deliverable in this mode."""
        bound = "<=" if kind == "episode" else "="  # episode lineage sits at the entering revision
        rows = tx._check().execute(
            f"SELECT DISTINCT source_ref,source_revision FROM evidence_links WHERE object_kind=? AND object_ref=? AND object_revision{bound}? ORDER BY source_ref,source_revision",
            (kind, ref, revision),
        ).fetchall()
        return self._deliverable(tx, tuple((row["source_ref"], row["source_revision"]) for row in rows), context)

    def _deliverable(self, tx, pairs, context: SearchContext) -> tuple[str, ...] | None:
        """The refs of these source versions, or ``None`` when one is not deliverable in this mode."""
        refs = []
        for source_ref, source_revision in pairs:
            source = tx.source(source_ref, source_revision)
            if source is None or (source.suppressed and context.mode == "auto"):
                return None
            if context.mode in LIVE_MODES and not _source_live(tx, source.ref, source.revision):
                return None
            refs.append(f"{source_ref}@{source_revision}")
        return tuple(refs)

    def hydrate(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        if not allowed(tx, candidate.kind, candidate.ref, automatic=context.mode == "auto"):
            return None
        load = {"event": self._hydrate_event, "claim": self._hydrate_claim}.get(candidate.kind, self._hydrate_versioned)
        return load(tx, candidate, context)

    def _hydrate_event(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        source = tx.source(candidate.ref, candidate.revision)
        if source is None or (context.mode == "auto" and source.suppressed):
            return None
        if context.mode in LIVE_MODES and not _source_live(tx, candidate.ref, candidate.revision):
            return None
        if not in_time_window(source.event.get("occurred_at"), context):
            return None
        newer_sql = "SELECT 1 FROM source_events newer WHERE newer.source_group_key=(SELECT source_group_key FROM source_events WHERE event_id=? AND source_revision=?) AND newer.source_revision>?"
        newer_params: list[object] = [candidate.ref, candidate.revision, candidate.revision]
        if context.as_of is not None:
            newer_sql += " AND (newer.occurred_at IS NULL OR newer.occurred_at<=?)"
            newer_params.append(context.as_of)
        superseded = tx._check().execute(newer_sql, newer_params).fetchone() is not None
        event = source.event
        context_meta = bounded_source_context(event.get("source_context"))
        return RetrievedObject(
            candidate.ref,
            candidate.revision,
            "event",
            event["content"],
            event["origin"],
            "historical" if superseded else "current",
            applicability(context, source.project_id, source.branch_id),
            (_source_key(candidate.ref, candidate.revision),),
            "direct_report" if event["origin"] == "human_direct" else "observed",
            True,
            ("event",),
            metadata=(*_source_contexts_metadata([context_meta] if context_meta is not None else []),
                      *_occurred_metadata(tx.witnessed_at(source)),
                      *((("recall_echo", "true"),) if context.mode in LIVE_MODES and recall_echo(tx, source) else ())),
        )

    def _hydrate_claim(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        versions = tx.claims.versions(candidate.ref)
        version = next((item for item in versions if item.revision == candidate.revision), None)
        if version is None or (context.mode == "auto" and version.suppressed):
            return None
        instant = context.as_of or context.now
        effective = select_effective(versions, instant, as_of=context.mode == "as_of")
        admitted_proposal = False
        if context.mode != "history":
            if effective is not None:
                if effective.revision != version.revision:
                    return None
            else:
                # A claim qualification never promoted has no effective version,
                # which on a real instance is almost the whole derived layer.
                # Admit the proposal head instead; it stays labelled below
                # (temporal_status "historical", claim_state and
                # qualification_reason in metadata) so it cannot be mistaken
                # for settled truth.  as_of stays excluded: a proposal answers
                # nothing about a past instant.
                proposal = None if context.mode == "as_of" else select_proposal(versions, instant)
                if proposal is None or proposal.revision != version.revision:
                    return None
                admitted_proposal = True
        payload = version.payload
        intention = payload.get("intention") if payload.get("kind") == "intention" else None
        if intention and intention.get("state") in CLOSED_INTENTION_STATES and context.mode in LIVE_MODES:
            return None
        evidence = self._evidence(tx, "claim", candidate.ref, candidate.revision, context)
        if evidence is None:
            return None
        if admitted_proposal and not _has_first_hand_root(tx, evidence):
            # Derived only from the system's own echo and never promoted:
            # recalling it would let the assistant's output become memory.
            return None
        current_effective = context.mode in LIVE_MODES and effective is not None and effective.revision == version.revision
        origins = []
        witnessed = []
        for ref in evidence:
            source = tx.source(*parse_source_ref(ref))
            if source is not None:
                origins.append(source.event["origin"])
                witnessed.append(tx.witnessed_at(source))
        origin = next((item for item in origins if item == "human_direct"), origins[0] if origins else "origin_unknown")
        metadata = (
            ("payload_json", _claim_content(version)),
            ("state", version.state),
            ("basis", version.basis),
            # Which gate rejected it, so a reader can tell a weak quote from an
            # unasserted question or a missing condition.
            ("qualification_reason", version.reason),
            *_source_contexts_metadata(evidence_source_contexts(tx, evidence)),
            # A claim was last said when its newest evidence was.
            *_occurred_metadata(_newest(witnessed)),
        )
        return RetrievedObject(
            candidate.ref,
            candidate.revision,
            "procedure" if payload.get("kind") == "procedure" else "claim",
            _claim_statement_content(version),
            origin,
            _claim_status(version, current_effective),
            applicability(context, version.project_id, version.branch_id),
            evidence,
            version.basis,
            True,
            ("claim",),
            metadata=metadata,
        )

    def _hydrate_versioned(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        """Episodes, artifacts and references: head-revision objects with evidence links."""
        repositories = {"episode": tx.episodes, "artifact": tx.artifacts, "reference": tx.references}
        obj = repositories[candidate.kind].get(candidate.ref, candidate.revision if context.mode in {"history", "as_of"} else None)
        if obj is None or (context.mode == "auto" and obj.suppressed) or obj.revision != candidate.revision:
            return None
        table, key = _HEAD_TABLES[candidate.kind]
        head = tx._check().execute(f"SELECT current_revision FROM {table} WHERE {key}=?", (candidate.ref,)).fetchone()
        if head is None:
            return None
        object_gaps = getattr(obj, "gaps", ())
        if context.mode in LIVE_MODES and any(gap in object_gaps for gap in STALE_RESUME_GAPS):
            return None
        if candidate.kind == "episode" and getattr(obj, "resume", None):
            # A resume is derived from what it cites.  The members are its
            # context, not its evidence, and a 200-member list is not a packet
            # item (fits_packet_schema): the cited versions are what is checked
            # for delivery and what the packet carries.
            refs = tuple(dict.fromkeys(ref for ref in resume_evidence_refs(obj.resume) if type(ref) is str and _is_source_ref(ref)))
            evidence = self._deliverable(tx, tuple(parse_source_ref(ref) for ref in refs), context)
        else:
            evidence = self._evidence(tx, candidate.kind, candidate.ref, candidate.revision, context)
        if evidence is None:
            return None
        content, basis, status_unknown = _versioned_body(candidate.kind, obj)
        if status_unknown:
            status = "unknown"
        else:
            status = "current" if candidate.revision == head["current_revision"] else "historical"
        metadata = [("gaps", json.dumps(tuple(object_gaps), ensure_ascii=False))]
        metadata.extend(_source_contexts_metadata(evidence_source_contexts(tx, evidence)))
        if candidate.kind == "episode":
            metadata.extend(self._episode_source_metadata(tx, candidate.ref, obj))
        applies = applicability(context, obj.project_id, obj.branch_id)
        if "environment_needs_revalidation" in object_gaps:
            status = "historical"
            applies += "; environment_needs_revalidation"
        return RetrievedObject(candidate.ref, candidate.revision, candidate.kind, content, basis, status, applies,
                               evidence, basis, True, (candidate.kind,), metadata=tuple(metadata))

    @staticmethod
    def _episode_source_metadata(tx, episode_id: str, obj) -> list[tuple[str, str]]:
        """Trusted event order and fresh text for the refs this resume retains.

        The episode table may hold more events than the relation budget; a late
        correction must not disappear merely because an unrelated earlier event
        consumed a LIMIT.  Only the schema's 32 retained refs are queried, by
        *versioned* pair, so no other revision of a source is ever read.
        """
        retained = [ref for ref in dict.fromkeys((*obj.evidence_refs, *resume_evidence_refs(obj.resume)))
                    if type(ref) is str and _is_source_ref(ref)][:32]
        rows = []
        texts: dict[str, str] = {}
        if retained:
            pairs = [parse_source_ref(ref) for ref in retained]
            pair_marks = ",".join("(?, ?)" for _ in pairs)
            rows = tx._check().execute(
                f"""SELECT sequence,source_ref,source_revision
                   FROM episode_events
                   WHERE episode_id=? AND (source_ref,source_revision) IN ({pair_marks})
                   ORDER BY sequence""",
                (episode_id, *(value for pair in pairs for value in pair)),
            ).fetchall()
            for row in rows:
                source = tx.source(row["source_ref"], row["source_revision"])
                if source is not None:
                    texts[_source_key(str(row["source_ref"]), int(row["source_revision"]))] = str(source.event.get("content", ""))
        order = [[str(row["source_ref"]), int(row["source_revision"]), int(row["sequence"])] for row in rows]
        return [
            ("source_order", json.dumps(order, ensure_ascii=False, separators=(",", ":"))),
            ("source_texts", json.dumps(texts, ensure_ascii=False, separators=(",", ":"))),
        ]

    # -- collection paging ----------------------------------------------------

    def collection(self, tx, context: SearchContext, query: CollectionQuery, cursor: PageCursor | None = None) -> CollectionPage:
        epoch = self.epoch(tx)
        expected_digest = scope_digest(context.trusted_context)
        if query.scope_digest != expected_digest or query.memory_epoch != epoch:
            raise ContractError("VERSION_CONFLICT", "collection_epoch")
        query_as_of = canonical_time(query.as_of) if query.as_of is not None else None
        if query.mode != context.mode or query_as_of != context.as_of:
            raise ContractError("ACCESS_DENIED", "collection_context")
        allowed_fields = {"ref", "scope_id", "project_id", "branch_id", "revision"} | {
            "claim": {"kind", "subject", "predicate", "state"},
            "episode": {"state"},
            "artifact": {"label"},
        }.get(query.object_kind, set())
        if any(key not in allowed_fields for key, _ in query.where):
            raise ContractError("INPUT_INVALID", "collection_where")
        trusted = context.trusted_context
        identity = (epoch, expected_digest, trusted.project_id, trusted.branch_id, context.mode, context.as_of, query.where, query.object_kind)
        if cursor is not None and (cursor.memory_epoch, cursor.scope_digest, cursor.project_id, cursor.branch_id,
                                   cursor.mode, cursor.as_of, cursor.filters, cursor.object_kind) != identity:
            raise ContractError("VERSION_CONFLICT", "cursor")
        hydrated: list[RetrievedObject] = []
        scan_cursor = cursor
        last: CandidateRef | None = None
        dropped = False
        has_more = False
        cut_short = False
        scan_budget = min(256, max(query.page_size + 1, query.page_size * 4))
        scanned = 0
        while len(hydrated) < query.page_size and scanned < scan_budget:
            if self._remaining(context) <= 0:
                cut_short = True
                break
            fetch_limit = min(query.page_size + 1, scan_budget - scanned)
            batch = self._collection_candidates(tx, context, query, scan_cursor, limit=fetch_limit)
            if not batch:
                break
            scanned += len(batch)
            has_more = len(batch) > query.page_size or (fetch_limit <= query.page_size and len(batch) >= fetch_limit)
            for candidate in batch[:query.page_size]:
                if self._remaining(context) <= 0:
                    cut_short = True
                    break
                last = candidate
                obj = self.hydrate(tx, candidate, context)
                if obj is None:
                    dropped = True
                else:
                    hydrated.append(obj)
                if len(hydrated) >= query.page_size:
                    break
            if last is not None:
                scan_cursor = PageCursor(*identity[:7], (last.kind, last.ref, last.revision), query.object_kind)
            if cut_short or not has_more:
                break
            if scanned >= scan_budget:
                cut_short = True
                break
        next_cursor = scan_cursor if (has_more or cut_short) and last is not None else None
        if next_cursor is not None or dropped or cut_short or cursor is not None:
            coverage = "partial"
        else:
            coverage = "complete_for_query"
        return CollectionPage(tuple(hydrated), next_cursor, coverage, epoch)

    def _collection_candidates(self, tx, context: SearchContext, query: CollectionQuery, cursor: PageCursor | None, *, limit: int | None = None) -> tuple[CandidateRef, ...]:
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        # Versioned objects are enumerated from their version tables: the parent
        # tables carry only the live head, so using them for history would omit
        # revisions and make state filters either invalid or falsely complete.
        table, ref_col, rev_col, parent_alias, version_alias = {
            "event": ("source_events e", "e.event_id", "e.source_revision", "e", "e"),
            "claim": ("claims c JOIN claim_versions v ON v.claim_id=c.claim_id", "c.claim_id", "v.revision", "c", "v"),
            "episode": ("episodes e JOIN episode_versions v ON v.episode_id=e.episode_id", "e.episode_id", "v.revision", "e", "v"),
            "artifact": ("artifacts a JOIN artifact_versions v ON v.artifact_id=a.artifact_id", "a.artifact_id", "v.revision", "a", "v"),
            "reference": ("reference_bindings b JOIN reference_versions v ON v.reference_id=b.reference_id", "b.reference_id", "v.revision", "b", "v"),
        }[query.object_kind]
        filters = [
            f"{parent_alias}.scope_id IN ({_marks(scopes)})",
            f"{parent_alias}.read_blocked=0",
            f"({parent_alias}.project_id IS NULL OR {parent_alias}.project_id=?)",
            f"({parent_alias}.branch_id IS NULL OR {parent_alias}.branch_id=?)",
            f"NOT EXISTS (SELECT 1 FROM object_blocks ob WHERE ob.object_kind=? AND ob.object_ref={ref_col} AND ob.read_blocked=1)",
        ]
        params: list[object] = [*scopes, context.trusted_context.project_id, context.trusted_context.branch_id, query.object_kind]
        for key, value in query.where:
            if key == "ref":
                column = ref_col
            elif key == "revision":
                column = rev_col
            else:
                column = f"{version_alias if key in {'state', 'label'} else parent_alias}.{key}"
            filters.append(f"{column}=?")
            if key == "revision":
                try:
                    params.append(int(value))
                except (TypeError, ValueError) as exc:
                    raise ContractError("INPUT_INVALID", "collection_where") from exc
            else:
                params.append(value)
        newer_sql = "SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision"
        if context.mode == "current":
            if query.object_kind == "event":
                filters.append(f"NOT EXISTS({newer_sql})")
            elif query.object_kind != "claim":
                filters.append(f"{rev_col}={parent_alias}.current_revision")
        elif context.mode == "as_of" and context.as_of is not None:
            if query.object_kind == "event":
                filters.append(f"({parent_alias}.occurred_at IS NULL OR {parent_alias}.occurred_at<=?)")
                filters.append(f"NOT EXISTS({newer_sql} AND (newer.occurred_at IS NULL OR newer.occurred_at<=?))")
                params.extend((context.as_of, context.as_of))
            elif query.object_kind != "claim":
                # Claims stay broad here; hydrate() applies the one temporal contract.
                filters.append(f"({version_alias}.recorded_at IS NULL OR {version_alias}.recorded_at<=?)")
                params.append(context.as_of)
        if cursor is not None:
            last = cursor.last_sort_key
            filters.append(f"({ref_col} > ? OR ({ref_col} = ? AND {rev_col} > ?))")
            params.extend((last[1], last[1], last[2]))
        fetch_limit = limit if limit is not None else query.page_size + 1
        rows = tx._check().execute(
            f"SELECT {ref_col} AS ref_sort,{rev_col} AS revision_sort FROM {table} WHERE {' AND '.join(filters)} ORDER BY {ref_col}, {rev_col} LIMIT ?",
            (*params, fetch_limit),
        ).fetchall()
        return tuple(CandidateRef(query.object_kind, row["ref_sort"], int(row["revision_sort"]), "exact_ref", rank=index) for index, row in enumerate(rows, 1))
