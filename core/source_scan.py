"""Checkpointed source screening on the existing local projection work queue.

Each item owns one complete message group. Computation uses a read snapshot;
publication rechecks its lease, exact body/metadata bytes and policy in a write.
No part becomes visible before the group's last window has been checked.
"""

import json

from .injection_guard import (
    _record_verdict,
    _remove_held_projections,
    blocked_refs,
    complete_events,
    evidence_verdict,
    group_input,
    group_rows,
    record_scan_deadline,
)
from .injection_policy import load_policy, policy_fingerprint, scan_events
from .source_admission import admit_source, observe_admitted
from .source_records import SourceWrite
from .worker_outcomes import budget_left, stale_result, work_result

WINDOWS_PER_PAGE = 16


def _read_group(tx, item):
    """Read withheld input under ordinary authorization and deletion fences."""
    from .visibility import deletion_allows

    rows = group_rows(tx, item.subject_ref, item.subject_revision)
    if not rows or any(
        row["scope_id"] not in tx.context.allowed_scope_ids
        or row["read_blocked"]
        or row["project_id"] not in {None, tx.context.project_id}
        or row["branch_id"] not in {None, tx.context.branch_id}
        or not deletion_allows(tx, "event", row["event_id"])
        for row in rows
    ):
        return ()
    return rows


def process_screening(storage, clock, context, item, *, started, budget, admission_policy=None):
    """Process at most one page, or return None for an ordinary projection item."""
    key = (item.subject_ref, item.subject_revision)
    with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        checkpoint = (
            tx._check()
            .execute("SELECT * FROM source_scan_progress WHERE source_ref=? AND source_revision=?", key)
            .fetchone()
        )
        if checkpoint is None:
            return None
        rows = _read_group(tx, item)
        if not rows:
            events, digest, policy, cursor = (), "", None, None
        else:
            events, digest = group_input(rows)
            try:
                policy = load_policy(tx._check())
            except (ValueError, TypeError, KeyError, RecursionError):
                policy = None
            cursor = (
                json.loads(checkpoint["cursor_json"])
                if (
                    policy is not None
                    and checkpoint["input_sha256"] == digest
                    and checkpoint["policy_sha256"] == policy.fingerprint
                )
                else None
            )
    verdict = (
        None
        if policy is None
        else scan_events(
            None,
            events,
            remaining_seconds=budget_left(started, clock, budget),
            policy=policy,
            progress=cursor,
            max_windows=WINDOWS_PER_PAGE,
        )
    )
    return _publish(
        storage,
        clock,
        context,
        item,
        (key, digest, policy),
        verdict,
        budget_left(started, clock, budget),
        admission_policy,
    )


def _publish(storage, clock, context, item, snapshot, verdict, remaining_seconds, admission_policy):
    from .injection_policy import ScanResult

    key, digest, policy = snapshot
    with storage.write(context, remaining_seconds=max(0.1, remaining_seconds)) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(*item.lease, now=now):
            return stale_result(tx, item)
        rows = _read_group(tx, item)
        if not rows:
            tx._check(write=True).execute(
                "DELETE FROM source_scan_progress WHERE source_ref=? AND source_revision=?", key
            )
            return work_result(tx.work.mark_obsolete(*item.lease, now=now))
        _events, current_digest = group_input(rows)
        current_policy_hash = policy_fingerprint(tx._check())
        events = complete_events(rows)
        if events is None:
            verdict = ScanResult("pending_review", ("incomplete_scan_group",))
        elif policy is None:
            verdict = ScanResult("pending_review", ("invalid_scan_policy",))
        elif digest != current_digest or policy.fingerprint != current_policy_hash:
            tx._check(write=True).execute(
                "UPDATE source_scan_progress SET input_sha256=?,policy_sha256=?,cursor_json='{}' "
                "WHERE source_ref=? AND source_revision=?",
                (current_digest, current_policy_hash, *key),
            )
            return work_result(
                tx.work.defer_without_attempt(*item.lease, now=now, seconds=0, error_code="scan_input_changed")
            )
        else:
            verdict = evidence_verdict(tx, events, verdict)
        if verdict.progress is not None:
            record_scan_deadline(tx, rows, verdict, now)
        _record_verdict(tx, rows, verdict, now)
        if verdict.progress is not None and verdict.state == "pending_review":
            tx._check(write=True).execute(
                "UPDATE source_scan_progress SET input_sha256=?,policy_sha256=?,cursor_json=? "
                "WHERE source_ref=? AND source_revision=?",
                (current_digest, current_policy_hash, json.dumps(verdict.progress), *key),
            )
            return work_result(
                tx.work.defer_without_attempt(*item.lease, now=now, seconds=0, error_code="scan_checkpoint")
            )
        tx._check(write=True).execute("DELETE FROM source_scan_progress WHERE source_ref=? AND source_revision=?", key)
        if verdict.state == "clear":
            _resume_group(tx, clock, rows[0]["event_id"], admission_policy)
        else:
            _remove_held_projections(tx, rows, preserve_work_id=item.work_id)
        return work_result(tx.work.complete(*item.lease, now=now))


def _resume_group(tx, clock, ref, policy):
    rows = group_rows(tx, ref)
    latest = max(row["source_revision"] for row in rows)
    held = blocked_refs(tx, "event", (row["event_id"] for row in rows))
    observed = []
    for row in rows:
        if row["event_id"] in held:
            continue
        decision = admit_source(
            tx, clock, row["event_id"], row["source_revision"], policy, schedule=row["source_revision"] == latest
        )
        if decision is not None:
            observed.append((SourceWrite("inserted", row["event_id"], latest), decision))
    observe_admitted(tx, clock, observed)
