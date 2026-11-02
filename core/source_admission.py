"""Source admission shared by capture, completed screening and owner release."""

import json

from .admission import AdmissionDecision, decide, store_decision
from .summary_storage import note_source


def admit_source(tx, clock, ref, revision, policy=None, *, schedule=True, new_activity=False):
    """Restore eligible projections without changing a preserved scheduling decision.

    Only a newly inserted capture version sets new_activity. Replay and safety
    repair preserve the recorded activity and a completed summary's end signal.
    """
    source = tx.source(ref, revision)
    if source is None:
        return None
    with tx._reuse_source(source):
        decision = _scheduling_decision(tx, source, policy) if schedule else None
        tx.claims.link_source(ref, revision)
        tx.sources.index_source(ref, revision)
        for kind in sorted(decision.work_types if decision is not None else ()):
            tx.sources.enqueue_source(ref, revision, work_type=kind, available_at=clock.utc_now())
        tx.episodes.attach(source, clock.utc_now())
        # A capture is activity; replay, review release and scan repair only
        # restore existing history. They must not reopen a completed session.
        note_source(
            tx,
            source,
            now=clock.utc_now() if new_activity else source.event["recorded_at"],
            historical=not new_activity,
        )
    return decision


def _scheduling_decision(tx, source, policy):
    """Keep existing terminal/deferred decisions independent from projection repair."""
    row = (
        tx._check()
        .execute(
            "SELECT extra_json FROM source_events WHERE event_id=? AND source_revision=?", (source.ref, source.revision)
        )
        .fetchone()
    )
    marker = json.loads(row[0]).get("_scope_recall_admission", {})
    if marker.get("disposition") in {"source_only", "deferred"}:
        return AdmissionDecision(marker["disposition"], marker["reason"], bool(marker.get("important")))
    decision = decide(tx, source.event, source.scope_id, policy, source_ref=source.ref, source_revision=source.revision)
    store_decision(tx, source.ref, source.revision, decision)
    return decision


def observe_admitted(tx, clock, sources):
    """Observe admitted, current, unsuppressed versions after immediate capture mutations."""
    triggers = []
    for source, decision in sources:
        stored = tx.source(source.ref, source.revision)
        current = tx.sources.source_current(source.ref)
        if (
            decision.disposition != "source_only"
            and stored is not None
            and not stored.suppressed
            and current is not None
            and current.revision == source.revision
        ):
            triggers.append(tx.candidates.observe_source(source.ref, source.revision, observed_at=clock.utc_now()))
    return triggers
