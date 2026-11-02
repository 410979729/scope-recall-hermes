"""Screen imported truth before its transaction publishes any derived content."""

from dataclasses import replace
from types import SimpleNamespace

from .injection_guard import group_rows, screen_sources
from .source_admission import admit_source, observe_admitted
from .source_records import SourceWrite


def screen_imported_sources(tx, names, now):
    """Rescan every imported group with the destination policy; preserve existing holds.

    Source stores supply no attestation of the destination policy or body bytes.
    Their clear/released verdicts therefore cannot authorize publication here.
    """
    conn = tx._check(write=True)
    for old in names.known:
        ref = names.event(old)
        conn.execute("DELETE FROM source_safety WHERE source_ref=? AND state!='quarantined'", (ref,))
    rows = conn.execute(
        """SELECT event_id,source_revision,source_group_key,project_id,branch_id
        FROM source_events WHERE entry_id=? ORDER BY source_revision,source_id""",
        (names.entry_id,),
    ).fetchall()
    seen = set()
    clock = SimpleNamespace(utc_now=lambda: now)
    for row in rows:
        identity = (row["source_group_key"], row["source_revision"])
        if identity in seen:
            continue
        seen.add(identity)
        view = tx.with_context(
            replace(tx.context, project_id=row["project_id"], branch_id=row["branch_id"]), writable=True
        )
        sources = tuple(
            SourceWrite("inserted", item["event_id"], item["source_revision"])
            for item in group_rows(view, row["event_id"], row["source_revision"])
        )
        eligible = screen_sources(view, sources, now)
        _admit_missing(view, clock, eligible)


def _admit_missing(tx, clock, eligible):
    """Restore projections and work omitted by an obsolete provenance-only hold."""
    observed = []
    for ref, revision in eligible:
        decision = admit_source(tx, clock, ref, revision)
        if decision is not None:
            observed.append((SourceWrite("inserted", ref, revision), decision))
    observe_admitted(tx, clock, observed)
