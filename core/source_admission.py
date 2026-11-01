"""Source admission shared by capture, completed screening and owner release."""

from .admission import decide, decision_marker, store_decision
from .visibility import allowed


def admit_source(tx, clock, ref, revision, policy=None):
    """Link, index and schedule an eligible version exactly once inside a write transaction."""
    if not allowed(tx, "event", ref) or decision_marker(tx, ref, revision) is not None:
        return None
    source = tx.source(ref, revision)
    if source is None:
        return None
    decision = decide(tx, source.event, source.scope_id, policy, source_ref=ref, source_revision=revision)
    store_decision(tx, ref, revision, decision)
    tx.claims.link_source(ref, revision)
    tx.sources.index_source(ref, revision)
    for kind in sorted(decision.work_types):
        tx.sources.enqueue_source(ref, revision, work_type=kind, available_at=clock.utc_now())
    tx.episodes.attach(source, clock.utc_now())
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
