"""The lexical index: a term dictionary and integer postings.

``lexical_projection`` stored every (term, event_id, source_revision) as
text, and its mirror index doubled that: on one instance 5.2 million rows
took 713 MB, half the store, for 258,000 distinct terms.  A term is now one
row in ``lexical_terms`` and a posting two integers in ``lexical_postings``
(``term_id``, ``source_id``) with a reverse index: 135 MB for the same rows,
and a document-frequency lookup in 18 ms.  ``source_id`` is a source
version's integer identity (``source_events.source_id``), assigned at insert;
the 1109 upgrade numbers the existing rows.

Every reader and writer of the index goes through here.  The three ranking
queries that join it (``storage.search_sources``, the lexical channel in
``retrieval_storage`` and the preference match in ``background_context``)
splice in ``JOIN`` below, so the tables are named in one place: filter on
``t.term``, aggregate on ``e``.
"""
from __future__ import annotations

from typing import Iterable

#: ``t`` is the term, ``p`` the posting, ``e`` the source version.
JOIN = ("lexical_terms t JOIN lexical_postings p ON p.term_id=t.term_id "
        "JOIN source_events e ON e.source_id=p.source_id")


def source_id(conn, event_id: str, source_revision: int) -> int | None:
    """The integer identity of one source version, or ``None`` when it does not exist."""
    row = conn.execute("SELECT source_id FROM source_events WHERE event_id=? AND source_revision=?",
                       (event_id, source_revision)).fetchone()
    return None if row is None else row[0]


def index_terms(conn, source: int, terms: Iterable[str]) -> int:
    """Record that the source holds these terms.  Returns how many postings were named."""
    unique = tuple(dict.fromkeys(terms))
    if not unique:
        return 0
    conn.executemany("INSERT OR IGNORE INTO lexical_terms(term) VALUES (?)", [(term,) for term in unique])
    conn.executemany(
        "INSERT OR IGNORE INTO lexical_postings(term_id,source_id) SELECT term_id,? FROM lexical_terms WHERE term=?",
        [(source, term) for term in unique],
    )
    return len(unique)


def terms_of(conn, source: int) -> tuple[str, ...]:
    """The terms recorded for one source version, in term order."""
    return tuple(row[0] for row in conn.execute(
        "SELECT t.term FROM lexical_postings p JOIN lexical_terms t ON t.term_id=p.term_id WHERE p.source_id=? ORDER BY t.term",
        (source,)))


def forget(conn, event_id: str) -> None:
    """Drop the postings of every version of the source (deletion)."""
    conn.execute("DELETE FROM lexical_postings WHERE source_id IN (SELECT source_id FROM source_events WHERE event_id=?)",
                 (event_id,))


def document_frequency(conn, terms: Iterable[str]) -> dict[str, int]:
    """How many source versions hold each term; a term nobody holds is absent."""
    wanted = tuple(terms)
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    return {row[0]: int(row[1]) for row in conn.execute(
        f"SELECT t.term,COUNT(*) FROM lexical_terms t JOIN lexical_postings p ON p.term_id=t.term_id "
        f"WHERE t.term IN ({marks}) GROUP BY t.term", wanted)}


__all__ = ["JOIN", "document_frequency", "forget", "index_terms", "source_id", "terms_of"]
