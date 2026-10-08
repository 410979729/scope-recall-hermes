"""Read-only SQLite collection and collection pagination; hydration is in ``retrieval_hydration``.

This module is intentionally coupled to the existing ``Transaction`` surface,
which remains the only authority for source visibility and versioned objects.
No function here writes, schedules work, increments counters, or releases text
outside the trusted context.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, cast

from ..contracts import (
    ContractError,
)
from . import lexical_index, lineage
from .claims import canonical_time, select_effective, select_proposal
from .delete_storage import canonical, retraction_after
from .events import lexical_terms
from .recall_policy import (
    claim_embedding_text,
    hard_identifiers,
    meaningful_query_terms,
    query_is_relevant,
    synonym_expansions,
)
from .recall_scope import says_something
from .retrieval import (
    CandidateRef,
    CollectionQuery,
    ObjectKind,
    PageCursor,
    RetrievedObject,
    SearchContext,
)
from .retrieval_hydration import RetrievalHydration, source_key
from .visibility import OBJECT_KINDS

_EXACT_REF = re.compile(r"(?:event|claim|episode|artifact|reference)-[A-Za-z0-9._/-]+@\d+")
#: A term in at least this share of all sources tells the ranker nothing: it
#: matches most of the corpus, so it separates nothing while costing the longest
#: posting list in the index.  Measured, the terms that clear this bar are JSON
#: field names from tool-observation envelopes, not anything a person wrote.
_LEXICAL_DF_FRACTION = 0.10
#: Floor so a young or small instance is never pruned: on a corpus of thirty
#: sources, "10% of everything" is three, and ordinary words would vanish.
_LEXICAL_DF_FLOOR = 64
#: Postings the lexical statement may group, the kept terms' document
#: frequencies added rarest first.  It groups every posting of every term, so its
#: time follows this sum: on the shared store a 2,000-character prompt's 80
#: terms held 273,000 postings and took 9 s, longer than the prompt's whole
#: recall, which then ran without its vector search as well.  The rarest
#: terms separate the most, and a question's own few are never cut.
_LEXICAL_POSTING_BUDGET = 20_000
_LEXICAL_MIN_TERMS = 16


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


def _discriminating_terms(tx, terms: tuple[str, ...], keep: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The terms the lexical statement searches (``_searched_terms``)."""
    return _searched_terms(tx, terms, keep)[0]


def _searched_terms(tx, terms: tuple[str, ...], keep: tuple[str, ...] = ()) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(the terms the lexical statement searches, those the posting budget left out of it).

    Drop query terms too common to separate anything.

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
    frequencies = lexical_index.document_frequency(conn, terms)
    if not frequencies:
        return terms, ()
    ceiling = _common_term_ceiling(conn)
    kept = tuple(term for term in terms if term in keep or frequencies.get(term, 0) < ceiling)
    if kept:
        searched = _within_posting_budget(kept, frequencies, keep)
        return searched, tuple(term for term in kept if term not in searched)
    rarest = min(frequencies.values())
    return tuple(term for term in terms if frequencies.get(term, 0) == rarest) or terms, ()


def _within_posting_budget(
    terms: tuple[str, ...], frequencies: dict[str, int], keep: tuple[str, ...]
) -> tuple[str, ...]:
    """The rarest ``_LEXICAL_MIN_TERMS`` of the terms more than one source holds, and more of them while their
    postings stay within ``_LEXICAL_POSTING_BUDGET``.  ``keep`` stays whatever it costs.  A term one source at most
    holds costs a posting at most and stays, but takes none of the rarest places: the prompt is stored before its own
    recall, and the words only it holds would have taken them all.  The query's own order is kept."""
    held = sorted(
        (term for term in terms if term not in keep and frequencies.get(term, 0) > 1),
        key=lambda term: (frequencies[term], term),
    )
    if len(held) <= _LEXICAL_MIN_TERMS:
        return terms
    spent = sum(frequencies.get(term, 0) for term in set(keep).intersection(terms))
    chosen: set[str] = set()
    for term in held:
        if len(chosen) >= _LEXICAL_MIN_TERMS and spent + frequencies[term] > _LEXICAL_POSTING_BUDGET:
            break
        chosen.add(term)
        spent += frequencies[term]
    return tuple(term for term in terms if term in chosen or term in keep or frequencies.get(term, 0) <= 1)


def _held_terms(tx, source_ids: list[int], terms: tuple[str, ...]) -> dict[int, set[str]]:
    """Which of ``terms`` each of these sources holds, in one look-up of the ``(source_id, term_id)`` index."""
    rows = (
        tx._check()
        .execute(
            f"SELECT p.source_id,t.term FROM lexical_postings p JOIN lexical_terms t ON t.term_id=p.term_id "
            f"WHERE p.source_id IN ({_marks(tuple(source_ids))}) AND t.term IN ({_marks(terms)})",
            (*source_ids, *terms),
        )
        .fetchall()
    )
    held: dict[int, set[str]] = {}
    for source_id, term in rows:
        held.setdefault(source_id, set()).add(term)
    return held


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
    frequencies = lexical_index.document_frequency(conn, live)
    if not frequencies:
        return {}
    ceiling = _common_term_ceiling(conn)
    return {term: original for term, original in live.items() if 0 < frequencies.get(term, 0) < ceiling}


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


def _said_by(stamp: str | None, end: str) -> bool:
    """Whether a stored time is at or before ``end`` (both ``canonical_time``); a time that cannot be read is not."""
    try:
        moment = canonical_time(stamp) if stamp else None
    except ContractError:
        return False
    return moment is not None and moment <= end


@dataclass(frozen=True)
class CollectionPage:
    items: tuple[RetrievedObject, ...]
    next_cursor: PageCursor | None
    coverage: str
    memory_epoch: int


#: How long after a person's message its turn's replies may come, and how many are followed.
TURN_REPLY_SECONDS = 1800
TURN_REPLY_LIMIT = 3
#: How long after a person's message a further message of theirs, sent before the agent's first reply, still joins
#: its turn (``RetrievalStorage._turn``).  The person adds to what they asked while the agent works, and the reply
#: answers both: of one person's 1,242 messages over two weeks, 141 had such a follow-up before a reply, 70 of them
#: inside the same host turn, and without this their turns read empty, so a question asked again never reached what
#: it had been told.  Nine in ten such follow-ups came within ten minutes; a later one may open a turn of its own.
TURN_FOLLOWUP_SECONDS = 600
#: Rows of one named day (and entries) the scoped channel reads before it chooses (``RetrievalStorage.scoped``), in
#: time order.  The shared store's busiest day was 861 messages of every entry, 542 of one; read only to 400 rows,
#: it lost its evening.
_SCOPED_SCAN_ROWS = 2000
#: Characters of a scoped message an automatic packet (4,096 units, six items) can still deliver beside others.
_SCOPED_ITEM_CHARS = 600
#: Characters up to which a message is read for whether it says anything of its day (``recall_scope.says_something``):
#: "继续", "好的，继续吧", "OK 继续执行" and "按你说的做" do not, and on a day of long prompts they filled the packet.
_SCOPED_SHORT_CHARS = 20


def _shares(counts: list[int], limit: int) -> list[int]:
    """``limit`` slots split evenly between days holding ``counts`` messages, a day with fewer handing the rest on."""
    shares = [0] * len(counts)
    left = limit
    open_days = [index for index, count in enumerate(counts) if count]
    while left > 0 and open_days:
        each = max(1, left // len(open_days))
        for index in list(open_days):
            given = min(each, counts[index] - shares[index], left)
            shares[index] += given
            left -= given
            if shares[index] >= counts[index]:
                open_days.remove(index)
            if left <= 0:
                break
    return shares


def _coarse_to_fine(items: list) -> list:
    """``items``, in time order, reordered so that any first part of them spreads across all of them: the first, the
    middle, the quarters, the eighths (the positions' bits read backwards).  Offered in time order, a packet of six
    took a day's morning and left its afternoon out."""
    bits = max(1, (len(items) - 1).bit_length())
    return [items[index] for index in sorted(range(len(items)), key=lambda index: int(f"{index:0{bits}b}"[::-1], 2))]


class RetrievalStorage(RetrievalHydration):
    """Typed read boundary used by one ``RetrievalPipeline`` instance."""

    def __init__(self, *, clock=None):
        self.clock = clock if clock is not None else time

    def _remaining(self, context: SearchContext) -> float:
        return context.deadline - self.clock.monotonic()

    def epoch(self, tx) -> int:
        return tx.memory_epoch()

    def retracted_since(self, tx, context: SearchContext, since: int) -> bool:
        """Whether a deletion or suppression in this recall's scopes was recorded after epoch ``since``."""
        return retraction_after(tx._check(), context.trusted_context.allowed_scope_ids, since)

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
        terms, cut = _searched_terms(tx, terms, keep=identifiers)
        # A synonym term matches as the query term it stands in for, so hits
        # and matched terms still count the query's own terms, once each.
        # Without a synonym the statement and its parameters are unchanged.
        synonyms = _discriminating_synonyms(tx, synonym_expansions(context.query), terms)
        credit = f"CASE t.term {' '.join('WHEN ? THEN ?' for _ in synonyms)} ELSE t.term END" if synonyms else "t.term"
        credits = tuple(value for pair in synonyms.items() for value in pair)
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        term_marks, scope_marks = _marks((*terms, *synonyms)), _marks(scopes)
        identified = f"MAX(t.term IN ({_marks(identifiers)})) DESC," if identifiers else ""
        current = (
            ""
            if context.mode in {"history", "as_of"}
            else "AND NOT EXISTS (SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)"
        )
        as_of = ""
        params: list[object] = [
            *terms,
            *synonyms,
            *scopes,
            context.trusted_context.project_id,
            context.trusted_context.branch_id,
        ]
        if context.as_of is not None:
            as_of = " AND (e.occurred_at IS NULL OR e.occurred_at<=?)"
            params.append(context.as_of)
        # The statement starts from the query's terms.  A store keeps no planner statistics, so SQLite weighs the terms
        # against the scopes by rule of thumb, and with a long enough query it started from the scope index instead:
        # every event of the audience read one by one, 21 s for a Telegram message of 72 characters on the shared
        # store, and the recall empty at its deadline.  ``+`` keeps the scope filter from choosing the index.
        rows = (
            tx._check()
            .execute(
                f"""SELECT e.event_id,e.source_revision,e.source_id,COUNT(DISTINCT {credit}) AS hits,
                       GROUP_CONCAT(DISTINCT hex({credit})) AS matched_term_hexes
                FROM {lexical_index.JOIN}
                WHERE t.term IN ({term_marks}) AND +e.scope_id IN ({scope_marks})
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
            )
            .fetchall()
        )
        # The posting budget chose which rows the statement found; what a found row holds of the terms it left out
        # still counts, as before: admission weighs a row's matches against the whole query.
        held = _held_terms(tx, [row["source_id"] for row in rows], cut) if cut and rows else {}
        candidates = []
        for index, row in enumerate(rows, 1):
            matched = {bytes.fromhex(encoded).decode("utf-8") for encoded in row["matched_term_hexes"].split(",")}
            matched.update(held.get(row["source_id"], ()))
            candidates.append(
                CandidateRef(
                    "event",
                    row["event_id"],
                    row["source_revision"],
                    "lexical",
                    rank=index,
                    lexical_score=float(len(matched)),
                    matched_query_terms=tuple(sorted(matched)),
                )
            )
        return tuple(candidates)

    def claims(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        """Claims whose own statement answers the query.

        The lexical, vector and recent channels all yield events, so a fact used
        to reach recall only through relation expansion out of an event that was
        retrieved first.  In a benchmark, 19 of the 29 facts recall missed were
        never reached at all: newer talk about the same subject filled the
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
        rows = (
            tx._check()
            .execute(
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
            )
            .fetchall()
        )
        instant = context.as_of or context.now
        scored = []
        prefetch = getattr(tx.claims, "prefetch_versions", None)
        if prefetch is not None:
            prefetch(row["claim_id"] for row in rows)
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
            CandidateRef(
                "claim",
                chosen.ref,
                chosen.revision,
                "claim_lexical",
                rank=index,
                lexical_score=float(len(hits)),
                matched_query_terms=tuple(sorted(hits)),
            )
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
            candidates.append(
                CandidateRef(cast(ObjectKind, kind), identity, revision, "exact_ref", rank=len(candidates) + 1)
            )
            if len(candidates) >= limit:
                break
        return tuple(candidates)

    def scoped(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        """The conversation of the days, and entries, that a question asking what was said then names
        (``recall_scope``).

        The person's own messages and the replies they were shown, in the question's audience, from the named days
        (and entries), spread evenly across each day so it is seen whole rather than its last hour: the person's
        messages of a length a packet can deliver beside others first, then their longer ones, then the replies.
        A short message that says nothing ("继续", "好的，继续吧") is not offered.  The days share the slots evenly,
        a day with fewer messages handing the rest on, and take turns in the order offered, so the first-named day
        does not fill the packet.  Only lengths are read, and the text of short messages.
        """
        scope = context.scope
        if scope is None or limit <= 0:
            return ()
        trusted = context.trusted_context
        scopes = tuple(sorted(trusted.allowed_scope_ids))
        entries = f"AND e.entry_id IN ({_marks(scope.entry_ids)})" if scope.entry_ids else ""
        current = (
            ""
            if context.mode in {"history", "as_of"}
            else "AND NOT EXISTS (SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)"
        )
        as_of = "AND e.occurred_at<=?" if context.as_of is not None else ""
        excluded = set(context.current_source_refs)
        days = []
        # One statement a day, so each reads the (scope, time) index for its own window.
        for start, end in scope.windows:
            rows = (
                tx._check()
                .execute(
                    f"""SELECT e.event_id,e.source_revision,e.role,length(e.content) AS size,
                           CASE WHEN length(e.content)<=? THEN e.content END AS short FROM source_events e
                    WHERE e.scope_id IN ({_marks(scopes)}) AND e.occurred_at>=? AND e.occurred_at<? {entries}
                      AND e.role IN ('user','assistant')
                      AND (e.origin IN ('human_direct','assistant_visible') OR (e.origin='imported'
                           AND e.import_provenance_sha256 IS NOT NULL
                           AND e.source_original_origin IN ('human_direct','assistant_visible')))
                      AND e.read_blocked=0 AND e.suppressed=0
                      AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)
                      AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                          AND b.object_ref=e.event_id AND (b.read_blocked=1 OR b.suppressed=1))
                      {current} {as_of}
                    ORDER BY e.occurred_at,e.rowid LIMIT ?""",
                    (
                        _SCOPED_SHORT_CHARS,
                        *scopes,
                        start,
                        end,
                        *scope.entry_ids,
                        trusted.project_id,
                        trusted.branch_id,
                        *((context.as_of,) if as_of else ()),
                        _SCOPED_SCAN_ROWS,
                    ),
                )
                .fetchall()
            )
            rows = [
                row
                for row in rows
                if source_key(row["event_id"], row["source_revision"]) not in excluded
                and (row["short"] is None or says_something(row["short"]))
            ]
            # A message longer than an automatic packet can hold beside others is left out of it whole (the compiler
            # never slices content), and on the coding clients' days most messages are: offered first, they were
            # dropped and other days' short items delivered instead.
            days.append(
                [
                    [row for row in rows if row["role"] == "user" and row["size"] <= _SCOPED_ITEM_CHARS],
                    [row for row in rows if row["role"] == "user" and row["size"] > _SCOPED_ITEM_CHARS],
                    [row for row in rows if row["role"] == "assistant" and row["size"] <= _SCOPED_ITEM_CHARS],
                ]
            )
        picks = []
        for tiers, share in zip(days, _shares([sum(map(len, tiers)) for tiers in days], limit)):
            day: list = []
            for tier in tiers:
                room = share - len(day)
                if room <= 0:
                    break
                step = max(1.0, len(tier) / room)
                day.extend(_coarse_to_fine([tier[int(position * step)] for position in range(min(room, len(tier)))]))
            picks.append(day)
        chosen = [day[turn] for turn in range(max(map(len, picks), default=0)) for day in picks if turn < len(day)]
        return tuple(
            CandidateRef("event", row["event_id"], row["source_revision"], "scoped", rank=index)
            for index, row in enumerate(chosen[:limit], 1)
        )

    def recent(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        trusted = context.trusted_context
        scopes = tuple(sorted(trusted.allowed_scope_ids))
        excluded = set(context.current_source_refs)
        # From the queue's pending rows (``work_ready``), never from every consolidation ever made: done items are
        # kept, and with no planner statistics SQLite read all of them by their work type (3,070 on the shared store,
        # growing with every consolidation) or, with one scope, every event of it.  ``+`` keeps those filters from
        # choosing an index; the rows are the same.
        rows = (
            tx._check()
            .execute(
                f"""SELECT e.event_id,e.source_revision,e.content,e.recorded_at
                FROM source_events e JOIN work_items w
                ON w.subject_ref=e.event_id AND w.subject_revision=e.source_revision
                WHERE +w.work_type='consolidate' AND w.state IN ('pending','leased')
                  AND e.session_id=? AND +e.scope_id IN ({_marks(scopes)})
                  AND (e.project_id IS NULL OR e.project_id=?)
                  AND (e.branch_id IS NULL OR e.branch_id=?)
                  AND e.read_blocked=0 AND e.suppressed=0
                  AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                      AND b.object_ref=e.event_id AND (b.read_blocked=1 OR b.suppressed=1))
                ORDER BY e.recorded_at DESC,e.event_id,e.source_revision DESC LIMIT ?""",
                (trusted.session_id, *scopes, trusted.project_id, trusted.branch_id, limit * 4),
            )
            .fetchall()
        )
        result = []
        for row in rows:
            key = source_key(row["event_id"], row["source_revision"])
            if key in excluded or not query_is_relevant(context.query, row["content"]):
                continue
            result.append(
                CandidateRef(
                    "event",
                    row["event_id"],
                    row["source_revision"],
                    "recent_raw",
                    rank=len(result) + 1,
                    lexical_score=1.0,
                )
            )
            if len(result) >= limit:
                break
        return tuple(result)

    def turn_replies(self, tx, candidate: CandidateRef) -> tuple[CandidateRef, ...]:
        """What the assistant said back in the turn a person's message opened: the first ``TURN_REPLY_LIMIT`` of the
        turn's replies (``_turn``)."""
        conn = tx._check()
        opening = self._opening(conn, candidate)
        return self._turn(conn, *opening, limit=TURN_REPLY_LIMIT, join=True)[0] if opening is not None else ()

    def latest_turn(self, tx, candidates, *, now: str) -> tuple[str, tuple[CandidateRef, ...], bool] | None:
        """Of the turns the person's messages ``candidates`` opened, the latest that received a reply: when it opened
        (UTC, ``canonical_time``), its replies, and whether they were read to the turn's end by ``now``.  The openings
        are read first and the turns newest first, so older copies of a question cost one look-up each; equal times
        fall back to capture order."""
        conn = tx._check()
        openings = [opening for candidate in candidates if (opening := self._opening(conn, candidate)) is not None]
        for row, opened in sorted(openings, key=lambda opening: (opening[1], opening[0]["rowid"]), reverse=True):
            replies, ended = self._turn(conn, row, opened, now=now)
            if replies:
                return opened, replies, ended
        return None

    @staticmethod
    def _opening(conn, candidate: CandidateRef):
        """The row of a message that opens a turn, the person's or one the host wrote into the conversation (a finished
        background process, which Hermes opens a turn with), and when (``canonical_time``); None for anything else."""
        if candidate.kind != "event":
            return None
        row = conn.execute(
            """SELECT rowid,scope_id,session_id,role,origin,occurred_at,content_sha256 FROM source_events
               WHERE event_id=? AND source_revision=?""",
            (candidate.ref, candidate.revision),
        ).fetchone()
        if (
            row is None
            or row["role"] != "user"
            or row["origin"] not in ("human_direct", "host_generated")
            or not row["occurred_at"]
        ):
            return None
        opened = canonical_time(row["occurred_at"])
        return (row, opened) if opened is not None else None

    @staticmethod
    def _turn(
        conn, row, opened: str, *, limit: int | None = None, now: str | None = None, join: bool = False
    ) -> tuple[tuple[CandidateRef, ...], bool]:
        """A turn's replies, at most ``limit`` of them, and whether they were read to its end by ``now`` (never, when
        cut at ``limit``, or with no reply or no ``now`` to judge by).

        Episode membership reaches a reply only through every event of its
        episode, in id order, so a recalled question used up the relation bound
        long before its own answer.  A turn's replies are the assistant's
        visible messages in the same scope and session after the message, in
        capture order, until the person speaks again, within the first 64 rows
        and ``TURN_REPLY_SECONDS``.  A gateway can capture a whole turn under
        one timestamp, so equal times fall back to rowid.  The turn was read to
        its end when the person spoke again within them, or when the rows ran
        out, its window has closed, and what the session says in the window
        after it, if anything, is the person's: an agent's turn of forty tool
        calls went past the 64 rows, and its last reply read was not its answer,
        nor is the last of a turn still going on.  That look
        is bounded to the next window, which the scope's time index reads in
        order: unbounded, it would read every later row of the scope.

        The same message stored again is not the person speaking again.  A
        Hermes provider rebuilt with its agent stored a turn's message a second
        time in older releases, with the reply, under the host's ordinal (most
        Hermes turns that seemed to have no reply were that), and the first copy
        would stop at the second before reaching the answer.
        With ``join``, neither is what the person adds before the agent's first
        reply, within ``TURN_FOLLOWUP_SECONDS``: the reply often answers both.
        Only the replies a turn offers as candidates join (``turn_replies``),
        which rank them like any other: the reply can answer a new request
        instead ("算了，先查值班表"), so it never leads an older copy to it
        (``latest_turn``), where the last reply is raised above the rest.
        A message the host writes into the conversation
        (a finished background process) ends a turn as the person's does: the
        rows do not say which turn its job began in.
        """
        window_end = (
            (datetime.fromisoformat(opened) + timedelta(seconds=TURN_REPLY_SECONDS))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        followup_end = (datetime.fromisoformat(opened) + timedelta(seconds=TURN_FOLLOWUP_SECONDS)).isoformat(
            timespec="microseconds"
        )
        rows = conn.execute(
            """SELECT event_id,source_revision,role,origin,content_sha256,occurred_at FROM source_events
               WHERE scope_id=? AND occurred_at>=? AND occurred_at<=? AND session_id=?
                 AND (occurred_at>? OR rowid>?) AND read_blocked=0 AND suppressed=0
               ORDER BY occurred_at,rowid LIMIT 64""",
            (row["scope_id"], row["occurred_at"], window_end, row["session_id"], row["occurred_at"], row["rowid"]),
        ).fetchall()
        replies: list[CandidateRef] = []
        for reply in rows:
            if reply["role"] == "user":
                if reply["origin"] == "human_direct" and reply["content_sha256"] == row["content_sha256"]:
                    continue
                if (
                    join
                    and not replies
                    and reply["origin"] == "human_direct"
                    and _said_by(reply["occurred_at"], followup_end)
                ):
                    continue
                return tuple(replies), True
            if reply["role"] == "assistant" and reply["origin"] == "assistant_visible":
                replies.append(
                    CandidateRef(
                        "event",
                        reply["event_id"],
                        int(reply["source_revision"]),
                        "relation",
                        rank=len(replies) + 1,
                        lexical_score=1.0,
                    )
                )
                if limit is not None and len(replies) >= limit:
                    return tuple(replies), False
        if (
            limit is not None
            or len(rows) >= 64
            or not replies
            or now is None
            or canonical_time(now) < canonical_time(window_end)
        ):
            return tuple(replies), False
        following = (
            (datetime.fromisoformat(opened) + timedelta(seconds=2 * TURN_REPLY_SECONDS))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        after = conn.execute(
            """SELECT role FROM source_events WHERE scope_id=? AND occurred_at>? AND occurred_at<=? AND session_id=?
                 AND read_blocked=0 AND suppressed=0 ORDER BY occurred_at LIMIT 1""",
            (row["scope_id"], window_end, following, row["session_id"]),
        ).fetchone()
        return tuple(replies), after is None or after["role"] == "user"

    def related(self, tx, candidate: CandidateRef, *, limit: int) -> tuple[CandidateRef, ...]:
        rows = lineage.related(tx._check(), candidate.kind, candidate.ref, limit=limit)
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

    # -- collection paging ----------------------------------------------------

    def collection(
        self, tx, context: SearchContext, query: CollectionQuery, cursor: PageCursor | None = None
    ) -> CollectionPage:
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
        identity = (
            epoch,
            expected_digest,
            trusted.project_id,
            trusted.branch_id,
            context.mode,
            context.as_of,
            query.where,
            query.object_kind,
        )
        if (
            cursor is not None
            and (
                cursor.memory_epoch,
                cursor.scope_digest,
                cursor.project_id,
                cursor.branch_id,
                cursor.mode,
                cursor.as_of,
                cursor.filters,
                cursor.object_kind,
            )
            != identity
        ):
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
            for candidate in batch[: query.page_size]:
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

    def _collection_candidates(
        self, tx, context: SearchContext, query: CollectionQuery, cursor: PageCursor | None, *, limit: int | None = None
    ) -> tuple[CandidateRef, ...]:
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        # Versioned objects are enumerated from their version tables: the parent
        # tables carry only the live head, so using them for history would omit
        # revisions and make state filters either invalid or falsely complete.
        table, ref_col, rev_col, parent_alias, version_alias = {
            "event": ("source_events e", "e.event_id", "e.source_revision", "e", "e"),
            "claim": ("claims c JOIN claim_versions v ON v.claim_id=c.claim_id", "c.claim_id", "v.revision", "c", "v"),
            "episode": (
                "episodes e JOIN episode_versions v ON v.episode_id=e.episode_id",
                "e.episode_id",
                "v.revision",
                "e",
                "v",
            ),
            "artifact": (
                "artifacts a JOIN artifact_versions v ON v.artifact_id=a.artifact_id",
                "a.artifact_id",
                "v.revision",
                "a",
                "v",
            ),
            "reference": (
                "reference_bindings b JOIN reference_versions v ON v.reference_id=b.reference_id",
                "b.reference_id",
                "v.revision",
                "b",
                "v",
            ),
        }[query.object_kind]
        filters = [
            f"{parent_alias}.scope_id IN ({_marks(scopes)})",
            f"{parent_alias}.read_blocked=0",
            f"({parent_alias}.project_id IS NULL OR {parent_alias}.project_id=?)",
            f"({parent_alias}.branch_id IS NULL OR {parent_alias}.branch_id=?)",
            f"NOT EXISTS (SELECT 1 FROM object_blocks ob WHERE ob.object_kind=? AND ob.object_ref={ref_col} AND ob.read_blocked=1)",
        ]
        params: list[object] = [
            *scopes,
            context.trusted_context.project_id,
            context.trusted_context.branch_id,
            query.object_kind,
        ]
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
        rows = (
            tx._check()
            .execute(
                f"SELECT {ref_col} AS ref_sort,{rev_col} AS revision_sort FROM {table} WHERE {' AND '.join(filters)} ORDER BY {ref_col}, {rev_col} LIMIT ?",
                (*params, fetch_limit),
            )
            .fetchall()
        )
        return tuple(
            CandidateRef(query.object_kind, row["ref_sort"], int(row["revision_sort"]), "exact_ref", rank=index)
            for index, row in enumerate(rows, 1)
        )
