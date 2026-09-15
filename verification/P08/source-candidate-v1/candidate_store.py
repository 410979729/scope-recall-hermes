"""Explicit storage helpers for event-derived memory candidates.

This module is the write boundary for event-digest candidates. The default
provider path keeps event extraction in dry-run mode; callers must explicitly
pass ``dry_run=False`` before candidate rows and governance audit events are
written.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from typing import Any, Iterable

from .candidate_extraction import ExtractedCandidate
from .capture_filters import classify_transport_noise
from .models import RuntimeScope
from .sql_store import record_governance_audit_event, store_row
from .sqlite_recovery import rollback_if_active


def _candidate_metadata(candidate: ExtractedCandidate) -> dict[str, Any]:
    metadata = dict(candidate.metadata or {})
    # Review receipts are written only by the explicit candidate-review
    # transaction.  An extractor/caller must not be able to smuggle one into a
    # fresh Event Digest row and thereby satisfy the later promotion gate.
    for key in (
        "admission_reviewed_at",
        "candidate_reviewed_at",
        "candidate_reviewed_by",
        "candidate_review_action",
        "candidate_promotion_batch_id",
        "promoted_at",
        "promoted_by",
        "promotion_reason",
    ):
        metadata.pop(key, None)
    # Admission identity and lifecycle are store-owned invariants. Candidate
    # extractor metadata is useful provenance but must not be able to promote a
    # row or forge a review receipt before it reaches the truth boundary.
    metadata.update({
        "origin_kind": "event_digest",
        "lifecycle": "candidate",
        "candidate_status": "needs_review",
        "review_status": "pending",
        "memory_type": candidate.memory_type,
        "confidence": candidate.confidence,
        "importance": min(0.8, max(0.2, float(candidate.confidence))),
        "evidence_refs": list(candidate.evidence_refs),
        "risk_flags": list(candidate.risk_flags),
        "digest_quality": {"recommended_action": "candidate"},
        "event_digest": True,
        "automatic_admission": {
            "source": "event_digest",
            "route": "memory_review",
            "reviewed": False,
        },
    })
    return metadata


def _cleanup_event_candidate_unit(
    conn: sqlite3.Connection,
    *,
    savepoint: str,
    savepoint_active: bool,
    owns_transaction: bool,
    original: BaseException,
) -> None:
    """Abandon this unit without hiding the original failure or caller writes.

    Admission, commit, and the returned receipt stay in ``store_event_candidates``.
    This helper only distinguishes owned outer rollback from a still-active
    local savepoint. Borrowed connections must not roll back the caller's
    earlier writes. A cleanup failure is attached as the cause so the
    connection remains fail-closed for the existing rollback/quarantine path.
    """

    cleanup_error: BaseException | None = None
    if savepoint_active:
        try:
            if bool(getattr(conn, "in_transaction", False)):
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException as exc:
            cleanup_error = exc
    if owns_transaction:
        try:
            rollback_if_active(conn)
        except BaseException as exc:
            if cleanup_error is not None and exc.__cause__ is None:
                exc.__cause__ = cleanup_error
            cleanup_error = exc
    if cleanup_error is not None:
        raise original from cleanup_error


def store_event_candidates(
    conn: sqlite3.Connection,
    *,
    candidates: Iterable[ExtractedCandidate],
    scope: RuntimeScope,
    scope_id: str,
    session_id: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Store one event-candidate batch atomically when explicitly enabled.

    This function owns admission (transport reject, dry-run, candidate
    metadata rewrite) and the write unit. It begins ``BEGIN IMMEDIATE`` only
    when the connection is idle; ``store_row`` and audit remain
    transaction-neutral. Companion audit rows are written in the same unit as
    inserted truth and are omitted on no-touch observation. Retry is not
    owned here. The returned report is the receipt; callers recover an
    unusable connection through the existing rollback/quarantine path.
    """

    received_candidates = list(candidates)
    rejection_reasons: Counter[str] = Counter()
    candidate_list: list[ExtractedCandidate] = []
    for candidate in received_candidates:
        transport = classify_transport_noise(candidate.content)
        if transport.blocked:
            rejection_reasons.update(
                f"transport_noise:{code}" for code in transport.reason_codes
            )
            continue
        candidate_list.append(candidate)
    report: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "planned": len(candidate_list),
        "rejected": len(received_candidates) - len(candidate_list),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "inserted": 0,
        "updated_existing": 0,
        "duplicates_no_touch": 0,
        "mutation_applied": False,
        "ids": [],
    }
    if dry_run or not candidate_list:
        return report

    owns_transaction = False
    savepoint_active = False
    savepoint = f"event_candidates_{uuid.uuid4().hex}"
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            owns_transaction = True
        conn.execute(f"SAVEPOINT {savepoint}")
        savepoint_active = True
        for candidate in candidate_list:
            memory_id = f"event-candidate-{uuid.uuid4().hex}"
            metadata = _candidate_metadata(candidate)
            stored_id, _summary, updated_at, inserted = store_row(
                conn,
                memory_id=memory_id,
                scope_id=scope_id,
                platform=scope.platform,
                user_id=scope.user_id,
                chat_id=scope.chat_id,
                thread_id=scope.thread_id,
                gateway_session_key=scope.gateway_session_key,
                agent_identity=scope.agent_identity,
                agent_workspace=scope.agent_workspace,
                session_id=session_id,
                source="event-digest",
                target=candidate.target,
                content=candidate.content,
                metadata=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                allow_duplicate=False,
                commit=False,
            )
            if inserted:
                report["inserted"] += 1
                action = "insert_candidate"
                report["mutation_applied"] = True
            else:
                # ``store_row`` guarantees candidate duplicate admission is a
                # zero-write observation.  Do not add an audit row here: the
                # audit itself would violate the zero-write/idempotence contract.
                report["duplicates_no_touch"] += 1
            report["ids"].append(stored_id)
            if not inserted:
                continue
            record_governance_audit_event(
                conn,
                event_id=f"event-candidate-audit-{uuid.uuid4().hex}",
                event_type="event_candidate",
                action=action,
                scope_id=scope_id,
                target_id=stored_id,
                before={},
                after={
                    "updated_at": updated_at,
                    "target": candidate.target,
                    "memory_type": candidate.memory_type,
                    "lifecycle": "candidate",
                },
                reason="event_digest_candidate_write",
                actor="scope-recall:event-digest",
                dry_run=False,
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        savepoint_active = False
        if owns_transaction:
            conn.commit()
    except BaseException as exc:
        _cleanup_event_candidate_unit(
            conn,
            savepoint=savepoint,
            savepoint_active=savepoint_active,
            owns_transaction=owns_transaction,
            original=exc,
        )
        raise
    return report
