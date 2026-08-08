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
from typing import Any, Iterable

from .candidate_extraction import ExtractedCandidate
from .memory_text_merge import curated_entry_covering_candidate
from .models import RuntimeScope
from .sql_store import curated_recall_item_id, record_governance_audit_event, store_row


def _candidate_metadata(candidate: ExtractedCandidate) -> dict[str, Any]:
    return {
        "lifecycle": "candidate",
        "memory_type": candidate.memory_type,
        "confidence": candidate.confidence,
        "importance": min(0.8, max(0.2, float(candidate.confidence))),
        "evidence_refs": list(candidate.evidence_refs),
        "risk_flags": list(candidate.risk_flags),
        "digest_quality": {"recommended_action": "candidate"},
        "event_digest": True,
        **dict(candidate.metadata or {}),
    }


def store_event_candidates(
    conn: sqlite3.Connection,
    *,
    candidates: Iterable[ExtractedCandidate],
    scope: RuntimeScope,
    scope_id: str,
    session_id: str,
    dry_run: bool = True,
    curated_entries: Iterable[tuple[str, str, str]] = (),
) -> dict[str, Any]:
    """Store one event-candidate batch atomically when explicitly enabled."""

    candidate_list = list(candidates)
    curated_entry_list = list(curated_entries)
    curated_matches = [
        curated_entry_covering_candidate(candidate.content, curated_entry_list)
        for candidate in candidate_list
    ]
    report: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "planned": len(candidate_list),
        "inserted": 0,
        "updated_existing": 0,
        "skipped_curated_duplicate": sum(match is not None for match in curated_matches),
        "ids": [],
        "curated_ids": [],
    }
    if dry_run or not candidate_list:
        return report

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    savepoint = f"event_candidates_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for candidate, curated_match in zip(candidate_list, curated_matches):
            if curated_match is not None:
                curated_target, curated_content, _updated_at = curated_match
                curated_id = curated_recall_item_id(curated_target, curated_content)
                report["curated_ids"].append(curated_id)
                record_governance_audit_event(
                    conn,
                    event_id=f"event-candidate-audit-{uuid.uuid4().hex}",
                    event_type="event_candidate",
                    action="skip_curated_duplicate",
                    scope_id=scope_id,
                    target_id=curated_id,
                    before={},
                    after={
                        "target": candidate.target,
                        "memory_type": candidate.memory_type,
                        "lifecycle": "candidate_rejected",
                        "evidence_refs": list(candidate.evidence_refs),
                    },
                    reason="curated memory covers candidate",
                    actor="scope-recall:event-digest",
                    dry_run=False,
                )
                continue
            memory_id = f"event-candidate-{uuid.uuid4().hex}"
            metadata = _candidate_metadata(candidate)
            stored_id, summary, updated_at, inserted = store_row(
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
                    "id": stored_id,
                    "summary": summary,
                    "updated_at": updated_at,
                    "target": candidate.target,
                    "memory_type": candidate.memory_type,
                    "lifecycle": "candidate",
                    "evidence_refs": list(candidate.evidence_refs),
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
