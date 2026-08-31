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


def store_event_candidates(
    conn: sqlite3.Connection,
    *,
    candidates: Iterable[ExtractedCandidate],
    scope: RuntimeScope,
    scope_id: str,
    session_id: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Store one event-candidate batch atomically when explicitly enabled."""

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
        "ids": [],
    }
    if dry_run or not candidate_list:
        return report

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    savepoint = f"event_candidates_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
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
            else:
                report["updated_existing"] += 1
                action = "dedupe_existing_candidate"
            report["ids"].append(stored_id)
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
        if started_outer_transaction:
            conn.commit()
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction and conn.in_transaction:
            conn.rollback()
        raise
    return report
