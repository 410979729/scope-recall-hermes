"""Evidence links and object dependencies, read and written in one place.

An object (event, claim, episode, artifact, reference) derives from source
versions (``evidence_links``) and may depend on other objects
(``object_dependencies``).  Every runtime reader and writer of those two
tables goes through here, so a rule such as "an episode's lineage sits at the
revision each source entered, and a revision reads every row at or below it"
is stated once instead of repeated in each query.  The legacy converters
(``maintenance/legacy_*``) write their own rows: they run outside a
Transaction on a target they are building.

Two queries stay where they are on purpose, because they join lineage into a
larger ranking query rather than read it: the preference match in
``core/background_context.py`` and the cited-origin lookup in
``core/candidate_tables.py``.
"""
from __future__ import annotations

_EVIDENCE_COLUMNS = "object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote"

#: Where an object's lineage sits.  Episode rows are written once, at the
#: revision the source entered (``Episodes.attach``); every other kind writes
#: its rows per revision.
_REVISION_BOUND = {"episode": "<="}

#: The revision a lineage row names, as relation expansion delivers it: an
#: episode at its head (the only revision the live modes hydrate), anything
#: else at the revision the row carries.
_HEAD_REVISION = ("CASE WHEN object_kind='episode' THEN COALESCE((SELECT current_revision FROM episodes "
                  "WHERE episode_id=object_ref),object_revision) ELSE object_revision END")


def revision_bound(kind: str) -> str:
    return _REVISION_BOUND.get(kind, "=")


# -- evidence links ------------------------------------------------------------

def link(conn, kind: str, ref: str, revision: int, source_ref: str, source_revision: int, *,
         relation: str = "derived_from", quote: str = "", location: str | None = None, once: bool = True) -> None:
    """Record that ``ref@revision`` derives from ``source_ref@source_revision``.

    ``once`` lets a repeated link pass silently; without it a duplicate is the
    integrity error it always was.
    """
    conn.execute(
        f"INSERT INTO evidence_links({_EVIDENCE_COLUMNS},location) VALUES (?,?,?,?,?,?,?,?)"
        + (" ON CONFLICT DO NOTHING" if once else ""),
        (kind, ref, revision, source_ref, source_revision, relation, quote, location),
    )


def link_unless_carried(conn, kind: str, ref: str, revision: int, source_ref: str, source_revision: int, *,
                        relation: str = "derived_from", quote: str = "") -> None:
    """Record the link unless the same source already backs this or an earlier revision.

    For a kind whose lineage is read at or below a revision (episodes), a
    source that entered earlier is already part of every later revision.
    """
    conn.execute(
        f"""INSERT INTO evidence_links({_EVIDENCE_COLUMNS}) SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS (
            SELECT 1 FROM evidence_links WHERE object_kind=? AND object_ref=? AND object_revision<=?
            AND source_ref=? AND source_revision=? AND relation=? AND quote=?)""",
        (kind, ref, revision, source_ref, source_revision, relation, quote,
         kind, ref, revision, source_ref, source_revision, relation, quote),
    )


def evidence(conn, kind: str, ref: str, revision: int) -> list[tuple[str, int]]:
    """The source versions ``ref@revision`` derives from, ordered and once each."""
    return [(row[0], row[1]) for row in conn.execute(
        f"SELECT DISTINCT source_ref,source_revision FROM evidence_links WHERE object_kind=? AND object_ref=? "
        f"AND object_revision{revision_bound(kind)}? ORDER BY source_ref,source_revision",
        (kind, ref, revision),
    )]


def sources_of(conn, kind: str, ref: str) -> list[str]:
    """Every source ref any revision of ``ref`` derives from."""
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT source_ref FROM evidence_links WHERE object_kind=? AND object_ref=?", (kind, ref))]


def dependents(conn, source_ref: str) -> list[tuple[str, str]]:
    """Every (kind, ref) that derives from any version of ``source_ref``."""
    return [(row[0], row[1]) for row in conn.execute(
        "SELECT DISTINCT object_kind,object_ref FROM evidence_links WHERE source_ref=?", (source_ref,))]


def purge_quotes(conn, kind: str, ref: str) -> None:
    """Drop every quote on the edges of ``ref`` and of its sources, keeping the graph's shape.

    Deletion removes what an object said; that it was derived from a source,
    and what was derived from it, stays visible so a later cascade still
    finds the dependents.
    """
    edges = conn.execute(
        """SELECT DISTINCT object_kind,object_ref,object_revision,source_ref,source_revision,relation
           FROM evidence_links WHERE (object_kind=? AND object_ref=?) OR source_ref=?""", (kind, ref, ref)).fetchall()
    conn.execute("DELETE FROM evidence_links WHERE (object_kind=? AND object_ref=?) OR source_ref=?", (kind, ref, ref))
    conn.executemany(
        f"INSERT INTO evidence_links({_EVIDENCE_COLUMNS}) VALUES (?,?,?,?,?,?,'') ON CONFLICT DO NOTHING",
        [tuple(edge) for edge in edges],
    )


# -- object dependencies ---------------------------------------------------------

def depend(conn, kind: str, ref: str, revision: int, dependency_kind: str, dependency_ref: str, dependency_revision: int) -> None:
    """Record that ``ref@revision`` depends on another object's version."""
    conn.execute("INSERT INTO object_dependencies VALUES (?,?,?,?,?,?)",
                 (kind, ref, revision, dependency_kind, dependency_ref, dependency_revision))


def depend_unless_carried(conn, kind: str, ref: str, revision: int, dependency_kind: str, dependency_ref: str,
                          dependency_revision: int) -> None:
    """Record the dependency unless this or an earlier revision already carries it (episodes)."""
    conn.execute(
        """INSERT INTO object_dependencies SELECT ?,?,?,?,?,? WHERE NOT EXISTS (
            SELECT 1 FROM object_dependencies WHERE object_kind=? AND object_ref=? AND object_revision<=?
            AND dependency_kind=? AND dependency_ref=? AND dependency_revision=?)""",
        (kind, ref, revision, dependency_kind, dependency_ref, dependency_revision,
         kind, ref, revision, dependency_kind, dependency_ref, dependency_revision),
    )


def dependents_on(conn, dependency_kind: str, dependency_ref: str) -> list[tuple[str, str]]:
    """Every (kind, ref) that depends on any version of the given object."""
    return [(row[0], row[1]) for row in conn.execute(
        "SELECT DISTINCT object_kind,object_ref FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?",
        (dependency_kind, dependency_ref))]


# -- relation expansion ------------------------------------------------------------

def related(conn, kind: str, ref: str, *, limit: int):
    """Objects one lineage step from ``ref``, in key order, as (object_kind, object_ref, object_revision) rows.

    Four directions in one ordered, bounded query: what derives from this
    source, what this object derives from, what it depends on, and what
    depends on it.  An episode is named at its head.
    """
    return conn.execute(
        f"""SELECT object_kind,object_ref,{_HEAD_REVISION} AS object_revision FROM evidence_links
           WHERE source_ref=? UNION SELECT 'event',source_ref,source_revision
           FROM evidence_links WHERE object_kind=? AND object_ref=?
           UNION SELECT dependency_kind,dependency_ref,dependency_revision
           FROM object_dependencies WHERE object_kind=? AND object_ref=?
           UNION SELECT object_kind,object_ref,{_HEAD_REVISION}
           FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?
           ORDER BY object_kind,object_ref,object_revision LIMIT ?""",
        (ref, kind, ref, kind, ref, kind, ref, limit),
    ).fetchall()


__all__ = ["depend", "depend_unless_carried", "dependents", "dependents_on", "evidence", "link",
           "link_unless_carried", "purge_quotes", "related", "revision_bound", "sources_of"]
