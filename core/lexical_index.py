"""The lexical index: a term dictionary and integer postings.

``lexical_projection`` stored every (term, event_id, source_revision) as
text, and its mirror index doubled that: on one instance 5.2 million rows
took 713 MB, half the store, for 258,000 distinct terms.  A term is now one
row in ``lexical_terms`` and a posting two integers in ``lexical_postings``
(``term_id``, ``source_id``) with a reverse index: 135 MB for the same rows,
and a document-frequency lookup in 18 ms.  ``source_id`` is a source
version's integer identity (``source_events.source_id``), assigned at insert;
the 1109 upgrade numbers the existing rows.

Every reader and writer of the index goes through here.  The two ranking
queries that start from the terms (``storage.search_sources`` and the lexical
channel in ``retrieval_storage``) splice in ``JOIN`` below, so the tables are
named in one place: filter on ``t.term``, aggregate on ``e``.  The preference
match in ``background_context`` starts from its few claims instead and names
the tables itself.
"""
from __future__ import annotations

import json
from typing import Iterable

from ..contracts import ContractError
from .events import WITHHELD_TOOL_OUTPUT_SQL, indexed_terms, withheld_tool_output

#: ``t`` is the term, ``p`` the posting, ``e`` the source version.
JOIN = ("lexical_terms t JOIN lexical_postings p ON p.term_id=t.term_id "
        "JOIN source_events e ON e.source_id=p.source_id")


def source_id(conn, event_id: str, source_revision: int) -> int | None:
    """The integer identity of one source version, or ``None`` when it does not exist."""
    row = conn.execute("SELECT source_id FROM source_events WHERE event_id=? AND source_revision=?",
                       (event_id, source_revision)).fetchone()
    return None if row is None else row[0]


def index_terms(conn, source: int, terms: Iterable[str]) -> int:
    """Record that the source holds these terms.  Returns how many postings were named.

    One statement each, whatever the count.  A statement per term handed the GIL back and forth at every row, and in a
    Hermes gateway whose other threads were busy each handoff waited out their switch interval: a tool output of 51,283
    characters (9,348 terms) held the store's writer lease 42 s, every other entry's write failed meanwhile, and Hermes
    skipped the tool hook for a minute (yuheng and tianshu, 2026-10-05).
    """
    unique = tuple(dict.fromkeys(terms))
    if not unique:
        return 0
    payload = json.dumps(unique, ensure_ascii=False)
    conn.execute("INSERT OR IGNORE INTO lexical_terms(term) SELECT value FROM json_each(?)", (payload,))
    conn.execute(
        "INSERT OR IGNORE INTO lexical_postings(term_id,source_id) "
        "SELECT term_id,? FROM lexical_terms WHERE term IN (SELECT value FROM json_each(?))",
        (source, payload))
    return len(unique)


def terms_of(conn, source: int) -> tuple[str, ...]:
    """The terms recorded for one source version, in term order: one row, whatever the count (``index_terms``)."""
    row = conn.execute(
        "SELECT json_group_array(t.term) FROM lexical_postings p JOIN lexical_terms t ON t.term_id=p.term_id "
        "WHERE p.source_id=?", (source,)).fetchone()
    # Python orders strings by code point, as SQLite's binary collation orders their UTF-8.
    return tuple(sorted(json.loads(row[0])))


def forget(conn, event_id: str) -> None:
    """Drop the postings of every version of the source (deletion)."""
    conn.execute("DELETE FROM lexical_postings WHERE source_id IN (SELECT source_id FROM source_events WHERE event_id=?)",
                 (event_id,))


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
            (SELECT term_id FROM lexical_terms WHERE term IN ({','.join('?' for _ in kept)}))""",
        (source, *kept)).rowcount


#: Placeholders one page may look at.  A page drops about ten postings each in one write transaction, and holds the
#: store's writer lease while it runs: a capture waits for that lease about a second.
WITHHELD_PAGE_MAX = 5000


def unindex_withheld(conn, scope_ids: Iterable[str], *, after_id: int, limit: int, dry_run: bool) -> dict:
    """One page of withheld tool outputs' placeholders that hold postings beyond their own terms, in ``source_id``
    order, and those postings dropped unless ``dry_run`` (#206).

    An older release, the 1109 upgrade and both imports indexed the whole placeholder; it is found now by its error
    text alone, when it carries one (``events.indexed_terms``).  The sources stay, and so do the postings of their
    error text.  The scan walks ``source_id``: the ``+`` keeps SQLite from starting at the role and scope index and
    sorting every tool row while the page holds the writer lease (review of 3.7.4).  A page cut short is found again,
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
              AND +e.scope_id IN ({','.join('?' for _ in scopes)})
              AND EXISTS (SELECT 1 FROM lexical_postings p WHERE p.source_id=e.source_id)
            ORDER BY e.source_id LIMIT ?""",
        (after_id, *scopes, limit + 1),
    ).fetchall()
    page = rows[:limit]
    whole: list[int] = []  # placeholders found by nothing
    beyond: list[tuple[int, tuple[str, ...]]] = []  # those found by their error text, holding more than its terms
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
        postings = 0 if not whole else conn.execute(
            f"SELECT COUNT(*) FROM lexical_postings WHERE source_id IN ({','.join('?' for _ in whole)})",
            whole).fetchone()[0]
        postings += sum(len(set(terms_of(conn, source)) - set(keep)) for source, keep in beyond)
    else:
        postings = unindex(conn, whole)
        for source, keep in beyond:
            postings += unindex_beyond(conn, source, keep)
            index_terms(conn, source, keep)
    return {"dry_run": dry_run, "sources": len(whole) + len(beyond), "postings": int(postings),
            "next_after_id": page[-1][0] if page else after_id, "more": len(rows) > limit}


def document_frequency(conn, terms: Iterable[str]) -> dict[str, int]:
    """How many source versions hold each term; a term nobody holds is absent."""
    wanted = tuple(terms)
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    return {row[0]: int(row[1]) for row in conn.execute(
        f"SELECT t.term,COUNT(*) FROM lexical_terms t JOIN lexical_postings p ON p.term_id=t.term_id "
        f"WHERE t.term IN ({marks}) GROUP BY t.term", wanted)}


__all__ = ["JOIN", "WITHHELD_PAGE_MAX", "document_frequency", "forget", "index_terms", "source_id", "terms_of", "unindex",
           "unindex_beyond", "unindex_withheld"]
