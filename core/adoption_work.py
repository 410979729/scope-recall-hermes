"""Durable adoption stages on the existing consolidation queue and lease."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ..contracts import ContractError
from .adoption_storage import adoption_context
from .entry_control import entry_fence, paused_since
from .worker_adoption import advance_adoption, publish_adoption
from .worker_outcomes import (
    budget_left,
    defer_model_pass,
    finalize_work,
    hold_for_entry_state,
    hold_for_private_mode,
    model_failure,
    stale_result,
)


def schedule_adoptions(tx, source_refs, *, now):
    """Checkpoint adjacent replies only after their ordinary consolidation commits.

    Reusing each source's completed work row keeps retries, ownership and deletion
    in the existing queue. A completed checkpoint prevents rescheduling on replay.
    """
    conn = tx._check(write=True)
    for key in source_refs:
        ref, revision = key.rsplit("@", 1)
        source = tx.source(ref, int(revision))
        snapshot = adoption_context(tx, source) if source is not None else None
        if snapshot is None:
            continue
        row = conn.execute(
            "SELECT work_id FROM work_items WHERE work_type='consolidate' "
            "AND subject_ref=? AND subject_revision=? AND state='done'",
            (ref, int(revision)),
        ).fetchone()
        if row is None:
            continue
        inserted = conn.execute(
            "INSERT OR IGNORE INTO adoption_processing(work_id,confirmation_ref,confirmation_revision,"
            "assistant_ref,assistant_revision,payload_json) VALUES (?,?,?,?,?,?)",
            (
                row[0],
                ref,
                int(revision),
                snapshot.assistant_ref,
                snapshot.assistant_revision,
                json.dumps(
                    dict(stage="locate", entry_fence=entry_fence(tx, (snapshot.confirmation, snapshot.assistant)))
                ),
            ),
        ).rowcount
        if inserted:
            conn.execute(
                "UPDATE work_items SET state='pending',attempt=0,available_at=?,last_error_code=NULL "
                "WHERE work_id=? AND state='done'",
                (now, row[0]),
            )


def adoption_checkpoint(tx, work_id):
    """Read one source-version checkpoint without mixing it with ordinary model input."""
    row = tx._check().execute("SELECT * FROM adoption_processing WHERE work_id=?", (work_id,)).fetchone()
    return dict(row) if row is not None else None


def _adoption_snapshot(storage, context, checkpoint, state, remaining):
    """Bind a checkpoint to the live sources and reset work interrupted by a pause."""
    with storage.read(context, remaining_seconds=remaining) as tx:
        owner = tx.source(checkpoint["confirmation_ref"], checkpoint["confirmation_revision"])
        snapshot = adoption_context(tx, owner) if owner is not None else None
        if snapshot is None or (snapshot.assistant_ref, snapshot.assistant_revision) != (
            checkpoint["assistant_ref"],
            checkpoint["assistant_revision"],
        ):
            raise ContractError("SOURCE_MISSING", "adoption_source")
        fence = state.get("entry_fence")
        if fence is None or paused_since(tx, fence):
            fence = entry_fence(tx, (snapshot.confirmation, snapshot.assistant))
            state = dict(stage="locate", entry_fence=fence)
        return snapshot, state, fence, paused_since(tx, fence)


def _save_adoption_stage(storage, clock, context, item, snapshot, fence, prepared, remaining):
    """Publish or discard under the lease and the same entry fence as dispatch."""
    state, result = prepared
    with storage.write(context, remaining_seconds=remaining) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(*item.lease, now=now):
            return stale_result(tx, item)
        if paused_since(tx, fence):
            state = dict(stage="locate", entry_fence=entry_fence(tx, (snapshot.confirmation, snapshot.assistant)))
            result = None
        else:
            state["entry_fence"] = fence
        if result is not None:
            publish_adoption(tx, replace(result, entry_fence=fence), now=now)
        tx._check(write=True).execute(
            "UPDATE adoption_processing SET payload_json=? WHERE work_id=?",
            (json.dumps(state, ensure_ascii=False), item.work_id),
        )
        if state["stage"] == "done":
            tx.work.complete(*item.lease, now=now)
            return "completed", None, "done"
        tx.work.defer_without_attempt(*item.lease, now=now, error_code="adoption_stage_pending", seconds=0)
        return "deferred", None, "pending"


def process_adoption(storage, clock, context, item, checkpoint, model, *, started, budget):
    """Advance one stage under the original lease; preserve paid results across passes."""

    def remaining():
        return max(0.001, budget_left(started, clock, budget))

    state: dict[str, Any] = json.loads(checkpoint["payload_json"])
    try:
        snapshot, state, fence, paused = _adoption_snapshot(storage, context, checkpoint, state, remaining())
        if paused:
            return hold_for_entry_state(storage, clock, context, item, started=started, budget=budget)
        if model is None or getattr(model, "judge", None) is None:
            raise ContractError("STORAGE_UNAVAILABLE", "model_unavailable")
        if state["stage"] != "publish":
            deferred = defer_model_pass(storage, clock, context, item, model, started=started, budget=budget)
            if deferred is not None:
                return deferred
        with storage.read(context, remaining_seconds=remaining()) as tx:
            interrupted = paused_since(tx, fence)
        if interrupted:
            return _save_adoption_stage(storage, clock, context, item, snapshot, fence, (state, None), remaining())
        next_state, result = advance_adoption(
            storage, context, snapshot, state, model.judge, clock=clock, remaining_seconds=remaining()
        )
        return _save_adoption_stage(storage, clock, context, item, snapshot, fence, (next_state, result), remaining())
    except ContractError as exc:
        if exc.field == "private_mode":
            return hold_for_private_mode(storage, clock, context, item, started=started, budget=budget)
        obsolete = exc.code in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}
        return finalize_work(
            storage,
            clock,
            context,
            item,
            "obsolete" if obsolete else "retry",
            "authority_revoked" if obsolete else (exc.field if exc.code == "STORAGE_UNAVAILABLE" else exc.code),
            started=started,
            budget=budget,
            error_detail=exc.field,
            stage="adoption_" + state["stage"],
        )
    except (ValueError, TypeError):
        return finalize_work(
            storage,
            clock,
            context,
            item,
            "retry",
            "derivation_invalid",
            started=started,
            budget=budget,
            error_detail="adoption_selection",
            stage="adoption_" + state["stage"],
        )
    except (RuntimeError, OSError) as exc:
        return finalize_work(
            storage,
            clock,
            context,
            item,
            *model_failure(exc),
            started=started,
            budget=budget,
            stage="adoption_" + state["stage"],
        )
