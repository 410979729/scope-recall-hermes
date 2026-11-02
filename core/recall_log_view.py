"""Scope-checked recall history and live visibility checks for its references."""

from __future__ import annotations

from dataclasses import replace

from ..contracts import ContractError
from . import private
from .injection_guard import held_state
from .model_content import withhold_secret_items
from .recall_trace import note_candidate
from .reflection_validity import observation_dependencies
from .retrieval import CandidateRef, SearchContext, SearchLimits
from .retrieval_storage import RetrievalStorage
from .visibility import allowed

REQUEST_PREFIX = "recall-request:"


def rejection_reason(tx, candidate, context):
    """Classify a known candidate using metadata; never store denied content."""
    table, key = {
        "event": ("source_events", "event_id"),
        "claim": ("claims", "claim_id"),
        "episode": ("episodes", "episode_id"),
        "artifact": ("artifacts", "artifact_id"),
        "reference": ("reference_bindings", "reference_id"),
    }[candidate.kind]
    kind_column = "kind" if candidate.kind == "claim" else "NULL"
    row = (
        tx._check()
        .execute(
            f"SELECT scope_id,project_id,branch_id,read_blocked,suppressed,{kind_column} FROM {table} WHERE {key}=?",
            (candidate.ref,),
        )
        .fetchone()
    )
    if row is None:
        return "missing"
    trusted = context.trusted_context
    if (
        row[0] not in trusted.allowed_scope_ids
        or row[1] not in (None, trusted.project_id)
        or row[2] not in (None, trusted.branch_id)
    ):
        return "permission"
    if row[5] == "observation":
        # Scoped identity survives a refused body; no denied payload is hydrated for its label.
        note_candidate(candidate, source_type="observation")
    if row[3]:
        return "deleted"
    if row[4]:
        return "muted"
    state = held_state(tx, candidate.kind, candidate.ref)
    if state is not None:
        return "pending_review" if state == "pending_review" else "quarantine"
    return _metadata_rejection(tx, candidate, context)


def _metadata_rejection(tx, candidate, context):
    """Explain complete observation dependencies with the ordinary source rejection interface."""
    dependencies = (
        observation_dependencies(tx, candidate.ref, candidate.revision) if candidate.kind == "claim" else None
    )
    if dependencies is not None:
        for member in dependencies[0]:
            reason = rejection_reason(
                tx, CandidateRef("event", member["ref"], member["revision"], "exact_ref"), context
            )
            if reason != "stale":
                return reason
    if _private_candidate(tx, candidate, context):
        return "private"
    if candidate.kind == "claim":
        version = (
            tx._check()
            .execute(
                "SELECT valid_to FROM claim_versions WHERE claim_id=? AND revision=?",
                (candidate.ref, candidate.revision),
            )
            .fetchone()
        )
        if version is not None and version[0] is not None and version[0] <= context.now:
            return "expired"
    return "stale"


def _private_candidate(tx, candidate, context):
    """Classify source privacy with the same policy used for live content exits."""
    intervals = private.active_intervals(tx, context.now)
    if private.source_blocked(intervals, None):
        return True
    if intervals:
        if candidate.kind == "event":
            sources = (
                tx._check()
                .execute(
                    "SELECT session_id,project_root,extra_json FROM source_events "
                    "WHERE event_id=? AND source_revision=?",
                    (candidate.ref, candidate.revision),
                )
                .fetchall()
            )
        else:
            sources = (
                tx._check()
                .execute(
                    "SELECT e.session_id,e.project_root,e.extra_json FROM evidence_links l JOIN source_events e "
                    "ON e.event_id=l.source_ref AND e.source_revision=l.source_revision "
                    "WHERE l.object_kind=? AND l.object_ref=? AND l.object_revision=?",
                    candidate.key,
                )
                .fetchall()
            )
        if any(private.source_blocked(intervals, source) for source in sources):
            return True
    return False


def _reference(tx, reader, context, item):
    """Expose only metadata for currently readable, safe, unsuppressed versions."""
    candidate = CandidateRef(item["kind"], item["ref"], item["revision"], "exact_ref")
    fresh = reader.hydrate(tx, candidate, context)
    current = fresh is not None
    if fresh is None and allowed(tx, candidate.kind, candidate.ref, automatic=True):
        # Superseded versions still identify which sessions saw an old value.
        # They never receive an action link or a copied historical body.
        fresh = reader.hydrate(tx, candidate, replace(context, mode="history"))
    if fresh is not None:
        sources = [tx.source(*_pair(ref)) for ref in fresh.evidence_refs]
        intervals = private.active_intervals(tx, context.now)
        if any(source is None or private.source_blocked(intervals, source) for source in sources):
            fresh = None
    if fresh is None or not withhold_secret_items([fresh], []):
        return dict(rank=item.get("rank"), reason=item["reason"], readable=False)
    return dict(item, readable=current, historical=not current)


def _pair(ref):
    name, revision = ref.rsplit("@", 1)
    return name, int(revision)


def visible_record(core, context, record, *, owner=False):
    """Filter metadata by recorded scope and recheck each reference in one fresh read."""
    if record["installation_id"] != context.binding.installation_id:
        return None
    if not set(record.get("scope_ids", ())) <= context.allowed_scope_ids:
        return None
    if not owner and (
        record["session_id"] != context.session_id
        or record.get("entry_id") != (context.entry_id or context.binding.agent_id)
        or record.get("project_id") != context.project_id
        or record.get("branch_id") != context.branch_id
    ):
        return None
    scoped = (
        replace(context, project_id=record.get("project_id"), branch_id=record.get("branch_id")) if owner else context
    )
    search = SearchContext(
        "recall history",
        "auto",
        None,
        (),
        SearchLimits(),
        core.clock.monotonic() + 2,
        core.clock.utc_now(),
        scoped,
        background_without_evidence=False,
    )
    reader = RetrievalStorage(clock=core.clock)
    with core.storage.read(scoped) as tx:
        if not owner and private.recall_state(tx, scoped, search.now)[0] is not None:
            raise ContractError("ACCESS_DENIED", "private_mode")
        items = [_reference(tx, reader, search, item) for item in record.get("items", ())]
        # Captured query references are just as private and deletable as recalled items.
        refs = []
        for ref in record.get("query", {}).get("refs", ()):
            name, revision = _pair(ref)
            if _reference(
                tx, reader, search, dict(kind="event", ref=name, revision=revision, reason="current_prompt")
            ).get("ref"):
                refs.append(ref)
    capture = dict(record.get("capture", {}), refs=refs)
    return dict(
        record,
        items=items,
        query=dict(record.get("query", {}), refs=refs)
        if "refs" in record.get("query", {})
        else record.get("query", {}),
        capture=capture,
    )


def history(core, context, *, session=None, request_id=None, before=None, limit=30):
    """Owner-authorized bounded pagination, newest first, without returning stored bodies."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or (before is not None and (type(before) is not int or before < 1))
    ):
        raise ContractError("INPUT_INVALID", "limit_or_before")
    for value in (session, request_id):
        if value is not None and (type(value) is not str or not 1 <= len(value) <= 240):
            raise ContractError("INPUT_INVALID", "session_or_request")
    core.recall_diagnostics.expire(core.clock.utc_now())
    core.recall_diagnostics.flush()
    rows = core.recall_diagnostics.store.list(
        installation_id=context.binding.installation_id,
        session_id=session,
        request_id=request_id,
        before=before,
        limit=limit + 1,
        now=core.clock.utc_now(),
    )
    items = [view for row in rows[:limit] if (view := visible_record(core, context, row, owner=True)) is not None]
    return dict(items=items, next_before=rows[limit - 1]["sequence"] if len(rows) > limit else None)
