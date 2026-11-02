"""Source and current-fact lexical indexes sharing a term dictionary.

``lexical_projection``, the table the 1109 upgrade replaces, stored every
(term, event_id, source_revision) as text, and its mirror index doubled that:
5.2 million rows took 713 MB, half a large store, for 258,000 distinct terms.
Here a term is one row in ``lexical_terms`` and a posting two integers in
``lexical_postings`` (``term_id``, ``source_id``) with a reverse index:
135 MB for the same rows, and a document-frequency lookup in 18 ms.
``source_id`` is a source version's integer identity (``source_events.source_id``),
assigned at insert; the 1109 upgrade numbers the existing rows.

Source writes, document frequency and dictionary lifetime are owned here. The two ranking
queries that start from the terms (``storage.search_sources`` and the lexical
channel in ``retrieval_storage``) splice in ``JOIN`` below, so the tables are
named in one place: filter on ``t.term``, aggregate on ``e``.  The preference
match in ``background_context`` starts from its few claims instead and names
the tables itself.

Current facts use ``(term_id, claim_id)`` postings, ordered by public claim ref
so bounded intersections need no whole-corpus tie sort. Their writers replace
only current head text in the caller's transaction; nomination and its scoped
policy live in ``retrieval_storage``. Dictionary cleanup accounts for both
posting tables.
"""

from __future__ import annotations

import json
from typing import Iterable

from ..contracts import ContractError, utc_timestamp
from .events import WITHHELD_TOOL_OUTPUT_SQL, indexed_terms, lexical_terms, withheld_tool_output

#: ``t`` is the term, ``p`` the posting, ``e`` the source version.
JOIN = "lexical_terms t JOIN lexical_postings p ON p.term_id=t.term_id JOIN source_events e ON e.source_id=p.source_id"


def source_id(conn, event_id: str, source_revision: int) -> int | None:
    """The integer identity of one source version, or ``None`` when it does not exist."""
    row = conn.execute(
        "SELECT source_id FROM source_events WHERE event_id=? AND source_revision=?", (event_id, source_revision)
    ).fetchone()
    return None if row is None else row[0]


def index_terms(conn, source: int, terms: Iterable[str]) -> int:
    """Record that the source holds these terms.  Returns how many postings were named.

    Batch statements avoid per-term Python/SQLite crossings and reduce time spent
    holding the writer lease under thread contention.
    """
    unique = tuple(dict.fromkeys(terms))
    if not unique:
        return 0
    payload = json.dumps(unique, ensure_ascii=False)
    conn.execute("INSERT OR IGNORE INTO lexical_terms(term) SELECT value FROM json_each(?)", (payload,))
    conn.execute(
        "INSERT OR IGNORE INTO lexical_postings(term_id,source_id) "
        "SELECT term_id,? FROM lexical_terms WHERE term IN (SELECT value FROM json_each(?))",
        (source, payload),
    )
    return len(unique)


def terms_of(conn, source: int) -> tuple[str, ...]:
    """The terms recorded for one source version, in term order: one row, whatever the count (``index_terms``)."""
    row = conn.execute(
        "SELECT json_group_array(t.term) FROM lexical_postings p JOIN lexical_terms t ON t.term_id=p.term_id "
        "WHERE p.source_id=?",
        (source,),
    ).fetchone()
    # Python orders strings by code point, as SQLite's binary collation orders their UTF-8.
    return tuple(sorted(json.loads(row[0])))


def forget(conn, event_id: str) -> None:
    """Drop the postings of every version of the source (deletion), and the terms no other source holds: left in the
    dictionary, they would keep the deleted words, an identifier whole."""
    versions = "SELECT source_id FROM source_events WHERE event_id=?"
    conn.execute(
        f"""DELETE FROM lexical_terms WHERE term_id IN
            (SELECT term_id FROM lexical_postings WHERE source_id IN ({versions}))
            AND NOT EXISTS (SELECT 1 FROM lexical_postings p WHERE p.term_id=lexical_terms.term_id
                AND p.source_id NOT IN ({versions}))
            AND NOT EXISTS (SELECT 1 FROM claim_lexical_postings p WHERE p.term_id=lexical_terms.term_id)""",
        (event_id, event_id),
    )
    conn.execute(f"DELETE FROM lexical_postings WHERE source_id IN ({versions})", (event_id,))


def unindex(conn, sources: Iterable[int]) -> int:
    """Drop every posting of these source versions, which stay.  Returns how many postings went."""
    ids = tuple(sources)
    if not ids:
        return 0
    return conn.execute(f"DELETE FROM lexical_postings WHERE source_id IN ({','.join('?' for _ in ids)})", ids).rowcount


def unindex_beyond(conn, source: int, keep: Iterable[str] = ()) -> int:
    """Drop the postings of one source version whose terms are not in ``keep``.  Returns how many went."""
    kept = tuple(keep)
    if not kept:
        return unindex(conn, (source,))
    return conn.execute(
        f"""DELETE FROM lexical_postings WHERE source_id=? AND term_id NOT IN
            (SELECT term_id FROM lexical_terms WHERE term IN ({",".join("?" for _ in kept)}))""",
        (source, *kept),
    ).rowcount


#: Placeholders one page may look at.  A page drops about ten postings each in one write transaction, and holds the
#: store's writer lease while it runs: a capture waits for that lease about a second.
WITHHELD_PAGE_MAX = 5000


def unindex_withheld(conn, scope_ids: Iterable[str], *, after_id: int, limit: int, dry_run: bool) -> dict:
    """One page of withheld tool outputs' placeholders that hold postings beyond their own terms, in ``source_id``
    order, and those postings dropped unless ``dry_run``.

    A placeholder whose every word was indexed holds postings beyond its own terms (``events.indexed_terms``:
    those of its error text, when it carries one).  The sources stay, and so do the postings of their
    error text.  The scan walks ``source_id``: the ``+`` keeps SQLite from starting at the role and scope index and
    sorting every tool row while the page holds the writer lease.  A page cut short is found again,
    and ``next_after_id`` continues the scan.
    """
    scopes = tuple(sorted(scope_ids))
    if type(after_id) is not int or after_id < 0:
        raise ContractError("INPUT_INVALID", "after_id")
    if type(limit) is not int or not 1 <= limit <= WITHHELD_PAGE_MAX:
        raise ContractError("INPUT_INVALID", "limit")
    if not scopes:
        return {"dry_run": dry_run, "sources": 0, "postings": 0, "next_after_id": after_id, "more": False}
    rows = conn.execute(
        f"""SELECT e.source_id,e.role,e.content FROM source_events e
            WHERE e.source_id>? AND +e.role='tool' AND {WITHHELD_TOOL_OUTPUT_SQL}
              AND +e.scope_id IN ({",".join("?" for _ in scopes)})
              AND EXISTS (SELECT 1 FROM lexical_postings p WHERE p.source_id=e.source_id)
            ORDER BY e.source_id LIMIT ?""",
        (after_id, *scopes, limit + 1),
    ).fetchall()
    page = rows[:limit]
    whole: list[int] = []  # placeholders indexed by nothing
    beyond: list[tuple[int, tuple[str, ...]]] = []  # those indexed by their error text, holding more than its terms
    for source, role, content in page:
        event = {"role": role, "content": content}
        if not withheld_tool_output(event):
            continue
        keep = indexed_terms(event)
        if not keep:
            whole.append(source)
        elif terms_of(conn, source) != keep:
            beyond.append((source, keep))
    if dry_run:
        postings = (
            0
            if not whole
            else conn.execute(
                f"SELECT COUNT(*) FROM lexical_postings WHERE source_id IN ({','.join('?' for _ in whole)})", whole
            ).fetchone()[0]
        )
        postings += sum(len(set(terms_of(conn, source)) - set(keep)) for source, keep in beyond)
    else:
        postings = unindex(conn, whole)
        for source, keep in beyond:
            postings += unindex_beyond(conn, source, keep)
            index_terms(conn, source, keep)
    return {
        "dry_run": dry_run,
        "sources": len(whole) + len(beyond),
        "postings": int(postings),
        "next_after_id": page[-1][0] if page else after_id,
        "more": len(rows) > limit,
    }


def document_frequency(conn, terms: Iterable[str]) -> dict[str, int]:
    """How many source versions hold each term; a term nobody holds is absent."""
    wanted = tuple(terms)
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    return {
        row[0]: int(row[1])
        for row in conn.execute(
            f"SELECT t.term,COUNT(*) FROM lexical_terms t JOIN lexical_postings p ON p.term_id=t.term_id "
            f"WHERE t.term IN ({marks}) GROUP BY t.term",
            wanted,
        )
    }


def _replace_claim_terms(conn, refs: tuple[str, ...], postings: list[tuple[str, str, int]], heads=()) -> None:
    """Replace a bounded set of heads once, then remove only dictionary words nobody holds.

    The same bulk boundary serves a single live head and an initialization page.
    All postings, dictionary changes and the caller's head or cursor commit
    together. Source postings keep their own identity and are never rewritten.
    """
    encoded = json.dumps(refs)
    previous = tuple(
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT term_id FROM claim_lexical_postings WHERE claim_id IN (SELECT value FROM json_each(?))",
            (encoded,),
        )
    )
    conn.execute("DELETE FROM claim_lexical_postings WHERE claim_id IN (SELECT value FROM json_each(?))", (encoded,))
    conn.execute("DELETE FROM claim_lexical_heads WHERE claim_id IN (SELECT value FROM json_each(?))", (encoded,))
    if heads:
        conn.execute(
            "INSERT INTO claim_lexical_heads(claim_id,is_simple,recorded) "
            "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(?)",
            (json.dumps(heads),),
        )
    if postings:
        terms = tuple(dict.fromkeys(term for _, term, _ in postings))
        conn.execute(
            "INSERT OR IGNORE INTO lexical_terms(term) SELECT value FROM json_each(?)",
            (json.dumps(terms, ensure_ascii=False),),
        )
        # The dictionary may have tiny pre-import statistics; JSON rows must
        # drive indexed term probes rather than a dictionary scan per posting.
        conn.execute(
            "INSERT INTO claim_lexical_postings(term_id,claim_id,subject_terms) "
            "SELECT t.term_id,json_extract(p.value,'$[0]'),json_extract(p.value,'$[2]') FROM json_each(?) p "
            "CROSS JOIN lexical_terms t ON t.term=json_extract(p.value,'$[1]')",
            (json.dumps(postings, ensure_ascii=False),),
        )
    if previous:
        conn.execute(
            "DELETE FROM lexical_terms WHERE term_id IN (SELECT value FROM json_each(?)) "
            "AND NOT EXISTS(SELECT 1 FROM lexical_postings p WHERE p.term_id=lexical_terms.term_id) "
            "AND NOT EXISTS(SELECT 1 FROM claim_lexical_postings p WHERE p.term_id=lexical_terms.term_id)",
            (json.dumps(previous),),
        )


def forget_claim(conn, claim_id: str) -> None:
    """Remove a fact's derived text, retaining words still held by any source or fact."""
    _replace_claim_terms(conn, (claim_id,), [])


def _claim_projection(rows) -> tuple[list, list]:
    """Project current text and mark subject words with the final scorer's denominator.

    A positive count marks a subject term; the same count on each such posting
    lets SQL check half-subject coverage without reading or tokenizing payloads.
    Single-character words do not contribute, as in final claim scoring.
    """
    postings, heads = [], []
    for row in rows:
        if row[4] == "alias" or row[5]:
            continue
        heads.append((row[0], int(row[6]), utc_timestamp(row[7]) if row[6] else ""))
        subject = {term for term in lexical_terms(str(row[1])) if len(term) > 1}
        for term in lexical_terms(" ".join(str(part or "") for part in row[1:4])):
            postings.append((row[0], term, len(subject) if term in subject else 0))
    return postings, heads


# A replayed deletion ledger can precede the head flag. Suppression stays
# indexed for explicit reads; either deletion marker excludes the head.
_CLAIM_HEAD = (
    "SELECT c.claim_id,c.subject,c.predicate,json_extract(v.payload_json,'$.value_text'),c.kind,"
    "(c.read_blocked OR EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='claim' "
    "AND b.object_ref=c.claim_id AND b.read_blocked=1)), "
    "CASE WHEN c.current_revision=1 AND v.state='active' AND v.valid_from IS NULL AND v.valid_to IS NULL "
    "AND coalesce(json_array_length(v.payload_json,'$.conditions'),0)=0 "
    "AND json_extract(v.payload_json,'$.subject')=c.subject "
    "AND json_extract(v.payload_json,'$.predicate')=c.predicate "
    "AND NOT EXISTS(SELECT 1 FROM claim_versions other WHERE other.claim_id=c.claim_id AND other.revision>1) "
    "THEN 1 ELSE 0 END,v.recorded_from "
    "FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision "
)


def index_claim(conn, claim_id: str) -> None:
    """Replace only the current head's subject, predicate and value in its writer's transaction.

    Reading the durable head keeps non-head proposals from replacing its words
    and makes ordinary writes, restore and backfill project identical text.
    """
    rows = conn.execute(_CLAIM_HEAD + "WHERE c.claim_id=?", (claim_id,)).fetchall()
    _replace_claim_terms(conn, (claim_id,), *_claim_projection(rows))


def claim_backfill_pending(conn) -> bool:
    """Whether initialization still owes durable pages of current-fact postings."""
    return (
        conn.execute(
            "SELECT 1 FROM candidate_scan_cursors WHERE cursor_name='claim_lexical' AND completed=0"
        ).fetchone()
        is not None
    )


def backfill_claim_page(conn, *, limit: int = 500) -> bool:
    """Index one primary-key page with its cursor atomically; return whether more pages remain.

    The initialization coordinator owns the writer lease across pages. A killed
    page rolls back its postings and cursor together; committed pages survive.
    Newly written facts use index_claim and need no cursor reset.
    """
    cursor = conn.execute(
        "SELECT position_ref FROM candidate_scan_cursors WHERE cursor_name='claim_lexical' AND completed=0"
    ).fetchone()
    if cursor is None:
        return False
    rows = conn.execute(
        _CLAIM_HEAD + "WHERE c.claim_id>? ORDER BY c.claim_id LIMIT ?", (cursor[0] or "", limit)
    ).fetchall()
    if rows:
        _replace_claim_terms(conn, tuple(row[0] for row in rows), *_claim_projection(rows))
    more = len(rows) == limit
    conn.execute(
        "UPDATE candidate_scan_cursors SET position_ref=?,processed_count=processed_count+?,completed=?,"
        "updated_at=datetime('now') WHERE cursor_name='claim_lexical'",
        (rows[-1][0] if rows else cursor[0], len(rows), int(not more)),
    )
    return more


__all__ = [
    "JOIN",
    "WITHHELD_PAGE_MAX",
    "backfill_claim_page",
    "claim_backfill_pending",
    "document_frequency",
    "forget_claim",
    "index_claim",
    "forget",
    "index_terms",
    "source_id",
    "terms_of",
    "unindex",
    "unindex_beyond",
    "unindex_withheld",
]
