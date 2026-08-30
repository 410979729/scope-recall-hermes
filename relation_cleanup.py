"""Backup-first operator cleanup for retired relation work and generated edges.

The default path is read-only.  Apply requires an exact, non-truncated plan
hash, a stable operation id, an activation maintenance lease, an online
verified backup, and compare-and-swap deletion of every selected row.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .maintenance_lease import (
    MaintenanceLeaseError,
    acquire_activation_lease,
    ensure_activation_guard_triggers,
    install_activation_lease_authorizer,
    release_activation_lease,
    remove_activation_guard_triggers,
)
from .operator_ledger import (
    mirror_operator_receipt,
    read_operator_operation,
    record_committed_operator_operation,
)
from .relation_frequency_index import relation_frequency_index_schema_status
from .relation_scope_state import (
    blocked_entities_receipt_hash,
    decode_blocked_entities_receipt,
)
from .sqlite_backup import verified_online_backup
from .truth_connection import connect_truth_database


RELATION_CLEANUP_OPERATION_KIND = "relation.legacy_cleanup"
_GENERATED_NOTE_PATTERN = "relation-extraction:%"
_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_QUEUE_STATUSES = frozenset({"pending", "retry", "processing", "dead_letter"})
_TERMINAL_STATES = frozenset({"cancelled", "superseded"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: Any) -> str:
    raw = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
        is not None
    )


def _verified_database_path(db_path: Path) -> Path:
    """Reject symlink/junction indirection before resolving an operator target."""

    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(db_path))))
    is_junction = getattr(os.path, "isjunction", lambda _value: False)
    for candidate in (lexical, *lexical.parents):
        if candidate.is_symlink() or bool(is_junction(candidate)):
            raise RuntimeError(
                "cleanup database path must not use symlink or junction indirection"
            )
    resolved = lexical.resolve(strict=False)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(lexical)):
        raise RuntimeError("cleanup database path resolution changed the target")
    return resolved


def _database_file_identity(path: Path) -> tuple[int, int]:
    """Return a stable identity for the exact regular truth file."""

    if not path.is_file():
        raise FileNotFoundError("SQLite truth DB not found")
    info = path.stat(follow_symlinks=False)
    return (int(info.st_dev), int(info.st_ino))


def _revalidate_database_target(
    path: Path,
    *,
    expected_identity: tuple[int, int],
) -> Path:
    """Close the validation/open TOCTOU window around maintenance fencing."""

    current = _verified_database_path(path)
    if _database_file_identity(current) != expected_identity:
        raise RuntimeError("cleanup database identity changed during maintenance setup")
    return current


def _verify_connection_target(conn: sqlite3.Connection, path: Path) -> None:
    """Prove SQLite opened the lexical truth path that was fenced."""

    rows = conn.execute("PRAGMA database_list").fetchall()
    main = next((str(row[2] or "") for row in rows if str(row[1]) == "main"), "")
    if not main:
        raise RuntimeError("cleanup SQLite connection has no main database path")
    opened = _verified_database_path(Path(main))
    if os.path.normcase(str(opened)) != os.path.normcase(str(path)):
        raise RuntimeError("cleanup SQLite connection opened an unexpected target")


def _verified_backup_directory(path: Path) -> Path:
    """Reject symlink/reparse indirection for backup evidence placement."""

    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    is_junction = getattr(os.path, "isjunction", lambda _value: False)
    for candidate in (lexical, *lexical.parents):
        if candidate.is_symlink() or bool(is_junction(candidate)):
            raise RuntimeError(
                "cleanup backup path must not use symlink or junction indirection"
            )
    resolved = lexical.resolve(strict=False)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(lexical)):
        raise RuntimeError("cleanup backup path resolution changed the target")
    if lexical.exists() and not lexical.is_dir():
        raise RuntimeError("cleanup backup directory is not a directory")
    return lexical


@dataclass(frozen=True)
class _QueueRow:
    id: int
    scope_id: str
    focus_memory_id: str
    requested_updated_at: str
    next_requested_updated_at: str
    reason: str
    status: str
    cursor_memory_id: str
    processed_pairs: int
    pass_processed_pairs: int
    pass_number: int
    supersession_count: int
    last_progress_at: str
    attempts: int
    lease_expirations: int
    pass_lease_expirations: int
    failures: int
    pass_failures: int
    available_at: str
    updated_at: str
    lease_owner: str
    lease_token: str
    lease_expires_at: str
    corpus_revision: int
    blocked_entities_json: str
    blocked_entities_sha256: str
    last_error: str
    created_at: str
    completed_at: str


@dataclass(frozen=True)
class _ReclassificationRow:
    scope_id: str
    active_revision: int
    next_revision: int
    status: str
    cursor_memory_id: str
    pass_processed_memories: int
    total_processed_memories: int
    pass_number: int
    requested_at: str
    updated_at: str
    completed_at: str


@dataclass(frozen=True)
class _GeneratedEdge:
    scope_id: str
    source_memory_id: str
    target_memory_id: str
    relation_type: str
    confidence: float
    note: str
    created_at: str


@dataclass(frozen=True)
class LegacyRelationCleanupPlan:
    """Exact private selectors plus a content-free public plan receipt."""

    scopes: tuple[str, ...]
    queue_statuses: tuple[str, ...]
    expected_target_revision: int | None
    terminal_state: str
    max_rows: int
    queue_rows: tuple[_QueueRow, ...]
    reclassification_rows: tuple[_ReclassificationRow, ...]
    generated_edges: tuple[_GeneratedEdge, ...]
    truncated: bool
    plan_sha256: str

    def public(self) -> dict[str, Any]:
        dispositions = {"cancelled": 0, "superseded": 0, "poisoned": 0}
        for row in self.queue_rows:
            if row.status == "dead_letter":
                dispositions["poisoned"] += 1
            elif row.next_requested_updated_at:
                dispositions["superseded"] += 1
                dispositions["cancelled"] += 1
            else:
                dispositions[self.terminal_state] += 1
        for row in self.reclassification_rows:
            if row.next_revision > 0:
                dispositions["superseded"] += 1
                dispositions["cancelled"] += 1
            else:
                dispositions[self.terminal_state] += 1
        return {
            "ok": not self.truncated,
            "status": "blocked_cap" if self.truncated else "planned",
            "dry_run": True,
            "plan_sha256": self.plan_sha256,
            "truncated": self.truncated,
            "scope_count": len(self.scopes),
            "rebuild_queue_count": len(self.queue_rows),
            "reclassification_count": len(self.reclassification_rows),
            "generated_edge_count": len(self.generated_edges),
            "selected_row_count": (
                len(self.queue_rows)
                + len(self.reclassification_rows)
                + len(self.generated_edges)
            ),
            "dispositions": dispositions,
        }


def _plan_document(
    *,
    scopes: tuple[str, ...],
    queue_statuses: tuple[str, ...],
    expected_target_revision: int | None,
    terminal_state: str,
    max_rows: int,
    queue_rows: Iterable[_QueueRow],
    reclassification_rows: Iterable[_ReclassificationRow],
    generated_edges: Iterable[_GeneratedEdge],
    truncated: bool,
) -> dict[str, Any]:
    return {
        "schema": "relation_legacy_cleanup_plan.v1",
        "scopes": list(scopes),
        "queue_statuses": list(queue_statuses),
        "expected_target_revision": expected_target_revision,
        "terminal_state": terminal_state,
        "max_rows": max_rows,
        "truncated": truncated,
        "queue": [
            {
                "id": row.id,
                "scope_id": row.scope_id,
                "focus_sha256": _sha(row.focus_memory_id),
                "requested_updated_at": row.requested_updated_at,
                "next_requested_updated_at": row.next_requested_updated_at,
                "reason_sha256": _sha(row.reason),
                "status": row.status,
                "cursor_sha256": _sha(row.cursor_memory_id),
                "processed_pairs": row.processed_pairs,
                "pass_processed_pairs": row.pass_processed_pairs,
                "pass_number": row.pass_number,
                "supersession_count": row.supersession_count,
                "last_progress_at": row.last_progress_at,
                "attempts": row.attempts,
                "lease_expirations": row.lease_expirations,
                "pass_lease_expirations": row.pass_lease_expirations,
                "failures": row.failures,
                "pass_failures": row.pass_failures,
                "available_at": row.available_at,
                "updated_at": row.updated_at,
                "lease_owner_sha256": _sha(row.lease_owner),
                "lease_token_sha256": _sha(row.lease_token),
                "lease_expires_at": row.lease_expires_at,
                "corpus_revision": row.corpus_revision,
                "blocked_entities_json_sha256": _sha(row.blocked_entities_json),
                "blocked_entities_sha256": row.blocked_entities_sha256,
                "last_error_sha256": _sha(row.last_error),
                "created_at": row.created_at,
                "completed_at": row.completed_at,
            }
            for row in queue_rows
        ],
        "reclassification": [
            {
                "scope_id": row.scope_id,
                "active_revision": row.active_revision,
                "next_revision": row.next_revision,
                "status": row.status,
                "cursor_sha256": _sha(row.cursor_memory_id),
                "pass_processed_memories": row.pass_processed_memories,
                "total_processed_memories": row.total_processed_memories,
                "pass_number": row.pass_number,
                "requested_at": row.requested_at,
                "updated_at": row.updated_at,
                "completed_at": row.completed_at,
            }
            for row in reclassification_rows
        ],
        "generated_edges": [
            {
                "scope_id": row.scope_id,
                "source_sha256": _sha(row.source_memory_id),
                "target_sha256": _sha(row.target_memory_id),
                "relation_type": row.relation_type,
                "confidence": row.confidence,
                "note_sha256": _sha(row.note),
                "created_at": row.created_at,
            }
            for row in generated_edges
        ],
    }


def plan_legacy_relation_cleanup(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str],
    queue_statuses: Iterable[str] | None = None,
    expected_target_revision: int | None = None,
    terminal_state: str = "cancelled",
    max_rows: int = 10_000,
) -> LegacyRelationCleanupPlan:
    """Plan exact legacy work and unverified generated edges with cap+1."""

    scopes = tuple(sorted({str(value).strip() for value in scope_ids if str(value).strip()}))
    if not scopes:
        raise ValueError("at least one exact scope_id is required")
    statuses = tuple(
        sorted(
            {
                str(value).strip().lower()
                for value in (queue_statuses or _QUEUE_STATUSES)
                if str(value).strip()
            }
        )
    )
    if not statuses or not set(statuses) <= _QUEUE_STATUSES:
        raise ValueError("queue_statuses must be explicit unresolved queue states")
    terminal = str(terminal_state or "").strip().lower()
    if terminal not in _TERMINAL_STATES:
        raise ValueError("terminal_state must be cancelled or superseded")
    bounded = max(1, min(int(max_rows), 100_000))
    required = {
        "relation_rebuild_queue",
        "relation_scope_reclassification",
        "relation_scope_containment",
        "relation_work_dispositions",
        "memory_relations",
        "memories",
    }
    missing = sorted(table for table in required if not _table_exists(conn, table))
    if missing:
        raise RuntimeError("relation cleanup schema is not current: " + ",".join(missing))

    scope_marks = ",".join("?" for _ in scopes)
    status_marks = ",".join("?" for _ in statuses)
    queue_revision_sql = ""
    queue_revision_params: tuple[Any, ...] = ()
    if expected_target_revision is not None:
        queue_revision_sql = " AND corpus_revision=?"
        queue_revision_params = (int(expected_target_revision),)
    queue_result = conn.execute(
        f"""
        SELECT id, scope_id, focus_memory_id, requested_updated_at,
               next_requested_updated_at, reason, status, cursor_memory_id,
               processed_pairs, pass_processed_pairs, pass_number,
               supersession_count, last_progress_at, attempts,
               lease_expirations, pass_lease_expirations, failures,
               pass_failures, available_at, updated_at, lease_owner,
               lease_token, COALESCE(lease_expires_at, ''), corpus_revision,
               blocked_entities_json, blocked_entities_sha256, last_error,
               created_at, COALESCE(completed_at, '')
        FROM relation_rebuild_queue
        WHERE scope_id IN ({scope_marks}) AND status IN ({status_marks})
              {queue_revision_sql}
        ORDER BY scope_id, id
        LIMIT ?
        """,
        (*scopes, *statuses, *queue_revision_params, bounded + 1),
    ).fetchall()
    truncated = len(queue_result) > bounded
    queue_result = queue_result[:bounded]
    queue_rows = tuple(
        _QueueRow(
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3] or ""),
            str(row[4] or ""),
            str(row[5] or ""),
            str(row[6] or ""),
            str(row[7] or ""),
            int(row[8] or 0),
            int(row[9] or 0),
            int(row[10] or 0),
            int(row[11] or 0),
            str(row[12] or ""),
            int(row[13] or 0),
            int(row[14] or 0),
            int(row[15] or 0),
            int(row[16] or 0),
            int(row[17] or 0),
            str(row[18] or ""),
            str(row[19] or ""),
            str(row[20] or ""),
            str(row[21] or ""),
            str(row[22] or ""),
            int(row[23] or 0),
            str(row[24] or ""),
            str(row[25] or ""),
            str(row[26] or ""),
            str(row[27] or ""),
            str(row[28] or ""),
        )
        for row in queue_result
    )

    remaining = max(0, bounded - len(queue_rows))
    revision_sql = ""
    revision_params: tuple[Any, ...] = ()
    if expected_target_revision is not None:
        revision_sql = " AND (active_revision=? OR next_revision=?)"
        revision_params = (
            int(expected_target_revision),
            int(expected_target_revision),
        )
    reclass_result = conn.execute(
        f"""
        SELECT scope_id, active_revision, next_revision, status,
               cursor_memory_id, pass_processed_memories,
               total_processed_memories, pass_number, requested_at,
               updated_at, COALESCE(completed_at, '')
        FROM relation_scope_reclassification
        WHERE scope_id IN ({scope_marks}) AND status<>'complete'{revision_sql}
        ORDER BY scope_id
        LIMIT ?
        """,
        (*scopes, *revision_params, remaining + 1),
    ).fetchall()
    if len(reclass_result) > remaining:
        truncated = True
    reclass_result = reclass_result[:remaining]
    reclassification_rows = tuple(
        _ReclassificationRow(
            str(row[0]),
            int(row[1] or 0),
            int(row[2] or 0),
            str(row[3]),
            str(row[4] or ""),
            int(row[5] or 0),
            int(row[6] or 0),
            int(row[7] or 0),
            str(row[8] or ""),
            str(row[9] or ""),
            str(row[10] or ""),
        )
        for row in reclass_result
    )

    remaining = max(0, bounded - len(queue_rows) - len(reclassification_rows))
    edge_result = conn.execute(
        f"""
        SELECT CASE WHEN s.scope_id IN ({scope_marks})
                    THEN s.scope_id ELSE t.scope_id END,
               r.source_memory_id, r.target_memory_id,
               r.relation_type, r.confidence, COALESCE(r.note, ''), r.created_at
        FROM memory_relations r
        JOIN memories s ON s.id=r.source_memory_id
        JOIN memories t ON t.id=r.target_memory_id
        WHERE (s.scope_id IN ({scope_marks}) OR t.scope_id IN ({scope_marks}))
          AND LOWER(COALESCE(r.note, '')) LIKE ?
        ORDER BY s.scope_id, r.source_memory_id,
                 r.target_memory_id, r.relation_type
        LIMIT ?
        """,
        (*scopes, *scopes, *scopes, _GENERATED_NOTE_PATTERN, remaining + 1),
    ).fetchall()
    if len(edge_result) > remaining:
        truncated = True
    edge_result = edge_result[:remaining]
    generated_edges = tuple(
        _GeneratedEdge(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            float(row[4] or 0.0),
            str(row[5] or ""),
            str(row[6] or ""),
        )
        for row in edge_result
    )
    document = _plan_document(
        scopes=scopes,
        queue_statuses=statuses,
        expected_target_revision=expected_target_revision,
        terminal_state=terminal,
        max_rows=bounded,
        queue_rows=queue_rows,
        reclassification_rows=reclassification_rows,
        generated_edges=generated_edges,
        truncated=truncated,
    )
    return LegacyRelationCleanupPlan(
        scopes,
        statuses,
        expected_target_revision,
        terminal,
        bounded,
        queue_rows,
        reclassification_rows,
        generated_edges,
        truncated,
        hashlib.sha256(_canonical_bytes(document)).hexdigest(),
    )


def cleanup_request_fingerprint(
    *,
    plan: LegacyRelationCleanupPlan,
    reason: str,
) -> str:
    """Bind operation id replay to selectors, plan, and a hashed reason."""

    return _cleanup_request_fingerprint_values(
        plan_sha256=plan.plan_sha256,
        scopes=plan.scopes,
        queue_statuses=plan.queue_statuses,
        expected_target_revision=plan.expected_target_revision,
        terminal_state=plan.terminal_state,
        max_rows=plan.max_rows,
        reason=reason,
    )


def _cleanup_request_fingerprint_values(
    *,
    plan_sha256: str,
    scopes: Iterable[str],
    queue_statuses: Iterable[str],
    expected_target_revision: int | None,
    terminal_state: str,
    max_rows: int,
    reason: str,
) -> str:
    payload = {
        "operation_kind": RELATION_CLEANUP_OPERATION_KIND,
        "plan_sha256": str(plan_sha256),
        "scopes": sorted({str(value) for value in scopes}),
        "queue_statuses": sorted({str(value) for value in queue_statuses}),
        "expected_target_revision": expected_target_revision,
        "terminal_state": str(terminal_state),
        "max_rows": int(max_rows),
        "reason_sha256": _sha(str(reason).strip()),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _insert_disposition(
    conn: sqlite3.Connection,
    *,
    work_kind: str,
    work_key: str,
    work_revision: str,
    scope_id: str,
    prior_status: str,
    prior_updated_at: str,
    terminal_state: str,
    attempts: int,
    lease_expirations: int,
    operation_id: str,
    request_fingerprint: str,
    disposed_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO relation_work_dispositions(
            work_kind, work_key, work_revision, scope_id, prior_status,
            prior_updated_at, terminal_state, reason_code, attempts,
            lease_expirations, operation_id, request_fingerprint, disposed_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'operator_legacy_cleanup', ?, ?, ?, ?, ?)
        """,
        (
            work_kind,
            work_key,
            work_revision,
            scope_id,
            prior_status,
            prior_updated_at,
            terminal_state,
            attempts,
            lease_expirations,
            operation_id,
            request_fingerprint,
            disposed_at,
        ),
    )


def _restore_scope_after_verified_cleanup(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    updated_at: str,
) -> bool:
    """Clear an operator block only when every trust/debt invariant is proven."""

    if not bool(relation_frequency_index_schema_status(conn).get("current")):
        return False
    row = conn.execute(
        """
        SELECT c.state, c.active_revision, c.target_revision,
               c.active_blocked_entities_json,
               c.active_blocked_entities_sha256,
               c.target_blocked_entities_json,
               c.target_blocked_entities_sha256,
               s.statistics_revision, s.corpus_revision,
               s.blocked_entities_json, s.blocked_entities_sha256,
               b.status, c.updated_at
        FROM relation_scope_containment c
        LEFT JOIN relation_scope_statistics s ON s.scope_id=c.scope_id
        LEFT JOIN relation_frequency_backfill b ON b.scope_id=c.scope_id
        WHERE c.scope_id=?
        """,
        (str(scope_id),),
    ).fetchone()
    if row is None or str(row[0]) != "blocked":
        return False
    active_revision = int(row[1] or 0)
    target_revision = int(row[2] or 0)
    statistics_revision = int(row[7] or -1)
    corpus_revision = int(row[8] or -2)
    active_json = str(row[3])
    target_json = str(row[5])
    statistics_json = str(row[9])
    active_entities = decode_blocked_entities_receipt(
        scope_id=str(scope_id),
        corpus_revision=active_revision,
        blocked_entities_json=active_json,
        blocked_entities_sha256=str(row[4] or ""),
    )
    target_entities = decode_blocked_entities_receipt(
        scope_id=str(scope_id),
        corpus_revision=target_revision,
        blocked_entities_json=target_json,
        blocked_entities_sha256=str(row[6] or ""),
    )
    statistics_entities = decode_blocked_entities_receipt(
        scope_id=str(scope_id),
        corpus_revision=corpus_revision,
        blocked_entities_json=statistics_json,
        blocked_entities_sha256=str(row[10] or ""),
    )
    receipts_match = (
        active_entities is not None
        and target_entities is not None
        and statistics_entities is not None
        and active_revision == target_revision
        and statistics_revision == corpus_revision
        and active_entities == target_entities == statistics_entities
        and active_json == target_json == statistics_json
        and str(row[4] or "")
        == blocked_entities_receipt_hash(scope_id, active_revision, active_entities)
        and str(row[6] or "")
        == blocked_entities_receipt_hash(scope_id, target_revision, target_entities)
        and str(row[10] or "")
        == blocked_entities_receipt_hash(scope_id, corpus_revision, statistics_entities)
        and str(row[11] or "") == "complete"
    )
    if not receipts_match:
        return False
    debt = conn.execute(
        """
        SELECT (
            EXISTS(
                SELECT 1 FROM memory_relations r
                JOIN memories source ON source.id=r.source_memory_id
                JOIN memories target ON target.id=r.target_memory_id
                WHERE (source.scope_id=? OR target.scope_id=?)
                  AND LOWER(COALESCE(r.note, '')) LIKE ?
            )
            OR EXISTS(
                SELECT 1 FROM relation_rebuild_queue q
                WHERE q.scope_id=? AND q.status<>'completed'
            )
            OR EXISTS(
                SELECT 1 FROM relation_scope_reclassification x
                WHERE x.scope_id=? AND x.status<>'complete'
            )
            OR EXISTS(
                SELECT 1
                FROM relation_focus_work_scopes s
                JOIN relation_focus_work w
                  ON w.memory_id=s.memory_id
                 AND w.work_generation=s.work_generation
                WHERE s.scope_id=?
            )
            OR EXISTS(
                SELECT 1 FROM relation_frequency_changes f
                WHERE f.old_scope_id=? OR f.new_scope_id=?
            )
            OR EXISTS(
                SELECT 1
                FROM relation_frequency_failures f
                LEFT JOIN relation_frequency_changes c ON c.memory_id=f.memory_id
                LEFT JOIN relation_indexed_memories i ON i.memory_id=f.memory_id
                LEFT JOIN memories m ON m.id=f.memory_id
                WHERE f.old_scope_id=? OR f.new_scope_id=?
                   OR c.old_scope_id=? OR c.new_scope_id=?
                   OR i.scope_id=? OR m.scope_id=?
            )
        )
        """,
        (
            scope_id,
            scope_id,
            _GENERATED_NOTE_PATTERN,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
            scope_id,
        ),
    ).fetchone()
    if debt is None or bool(debt[0]):
        return False
    return (
        conn.execute(
            """
            UPDATE relation_scope_containment
            SET state='ready', reason_code='', affected_count=0,
                item_total=0, completed_items=0, next_attempt_at='', updated_at=?
            WHERE scope_id=? AND state='blocked' AND active_revision=?
              AND target_revision=? AND active_blocked_entities_json=?
              AND active_blocked_entities_sha256=?
              AND target_blocked_entities_json=?
              AND target_blocked_entities_sha256=? AND updated_at=?
            """,
            (
                updated_at,
                scope_id,
                active_revision,
                target_revision,
                active_json,
                str(row[4] or ""),
                target_json,
                str(row[6] or ""),
                str(row[12] or ""),
            ),
        ).rowcount
        == 1
    )


def apply_legacy_relation_cleanup(
    conn: sqlite3.Connection,
    *,
    plan: LegacyRelationCleanupPlan,
    operation_id: str,
    request_fingerprint: str,
    backup_path: str,
) -> dict[str, Any]:
    """Apply one exact plan inside the caller's BEGIN IMMEDIATE transaction."""

    if plan.truncated:
        raise ValueError("truncated cleanup plan cannot be applied")
    disposed_at = _now_iso()
    dispositions = {"cancelled": 0, "superseded": 0, "poisoned": 0}
    for row in plan.queue_rows:
        terminal = "poisoned" if row.status == "dead_letter" else plan.terminal_state
        if row.next_requested_updated_at:
            terminal = "superseded"
        current_revision = row.requested_updated_at or f"corpus:{row.corpus_revision}"
        _insert_disposition(
            conn,
            work_kind="rebuild_queue",
            work_key=str(row.id),
            work_revision=current_revision,
            scope_id=row.scope_id,
            prior_status=row.status,
            prior_updated_at=row.updated_at,
            terminal_state=terminal,
            attempts=row.attempts,
            lease_expirations=row.lease_expirations,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
            disposed_at=disposed_at,
        )
        dispositions[terminal] += 1
        if row.next_requested_updated_at:
            _insert_disposition(
                conn,
                work_kind="rebuild_queue",
                work_key=str(row.id),
                work_revision=row.next_requested_updated_at,
                scope_id=row.scope_id,
                prior_status=row.status,
                prior_updated_at=row.updated_at,
                terminal_state="cancelled",
                attempts=0,
                lease_expirations=0,
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                disposed_at=disposed_at,
            )
            dispositions["cancelled"] += 1
        changed = conn.execute(
            """
            DELETE FROM relation_rebuild_queue
            WHERE id=? AND scope_id=? AND focus_memory_id=?
              AND requested_updated_at=? AND next_requested_updated_at=?
              AND reason=? AND status=? AND cursor_memory_id=?
              AND processed_pairs=? AND pass_processed_pairs=?
              AND pass_number=? AND supersession_count=?
              AND last_progress_at=? AND attempts=? AND lease_expirations=?
              AND pass_lease_expirations=? AND failures=? AND pass_failures=?
              AND available_at=? AND updated_at=?
              AND lease_owner=? AND lease_token=?
              AND COALESCE(lease_expires_at, '')=?
              AND corpus_revision=? AND blocked_entities_json=?
              AND blocked_entities_sha256=? AND last_error=? AND created_at=?
              AND COALESCE(completed_at, '')=?
            """,
            (
                row.id,
                row.scope_id,
                row.focus_memory_id,
                row.requested_updated_at,
                row.next_requested_updated_at,
                row.reason,
                row.status,
                row.cursor_memory_id,
                row.processed_pairs,
                row.pass_processed_pairs,
                row.pass_number,
                row.supersession_count,
                row.last_progress_at,
                row.attempts,
                row.lease_expirations,
                row.pass_lease_expirations,
                row.failures,
                row.pass_failures,
                row.available_at,
                row.updated_at,
                row.lease_owner,
                row.lease_token,
                row.lease_expires_at,
                row.corpus_revision,
                row.blocked_entities_json,
                row.blocked_entities_sha256,
                row.last_error,
                row.created_at,
                row.completed_at,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("relation rebuild cleanup CAS drift")

    for row in plan.reclassification_rows:
        terminal = "superseded" if row.next_revision > 0 else plan.terminal_state
        _insert_disposition(
            conn,
            work_kind="scope_reclassification",
            work_key=row.scope_id,
            work_revision=str(row.active_revision),
            scope_id=row.scope_id,
            prior_status=row.status,
            prior_updated_at=row.updated_at,
            terminal_state=terminal,
            attempts=max(0, row.pass_number - 1),
            lease_expirations=0,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
            disposed_at=disposed_at,
        )
        dispositions[terminal] += 1
        if row.next_revision > 0:
            _insert_disposition(
                conn,
                work_kind="scope_reclassification",
                work_key=row.scope_id,
                work_revision=str(row.next_revision),
                scope_id=row.scope_id,
                prior_status=row.status,
                prior_updated_at=row.updated_at,
                terminal_state="cancelled",
                attempts=0,
                lease_expirations=0,
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                disposed_at=disposed_at,
            )
            dispositions["cancelled"] += 1
        changed = conn.execute(
            """
            DELETE FROM relation_scope_reclassification
            WHERE scope_id=? AND active_revision=? AND next_revision=?
              AND status=? AND cursor_memory_id=?
              AND pass_processed_memories=? AND total_processed_memories=?
              AND pass_number=? AND requested_at=? AND updated_at=?
              AND COALESCE(completed_at, '')=?
            """,
            (
                row.scope_id,
                row.active_revision,
                row.next_revision,
                row.status,
                row.cursor_memory_id,
                row.pass_processed_memories,
                row.total_processed_memories,
                row.pass_number,
                row.requested_at,
                row.updated_at,
                row.completed_at,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("relation reclassification cleanup CAS drift")

    for row in plan.generated_edges:
        changed = conn.execute(
            """
            DELETE FROM memory_relations
            WHERE source_memory_id=? AND target_memory_id=? AND relation_type=?
              AND confidence=? AND COALESCE(note, '')=? AND created_at=?
            """,
            (
                row.source_memory_id,
                row.target_memory_id,
                row.relation_type,
                row.confidence,
                row.note,
                row.created_at,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("generated relation cleanup CAS drift")

    restored_scope_count = sum(
        int(
            _restore_scope_after_verified_cleanup(
                conn,
                scope_id=scope,
                updated_at=disposed_at,
            )
        )
        for scope in plan.scopes
    )

    before = {
        "plan_sha256": plan.plan_sha256,
        "scope_count": len(plan.scopes),
        "rebuild_queue_count": len(plan.queue_rows),
        "reclassification_count": len(plan.reclassification_rows),
        "generated_edge_count": len(plan.generated_edges),
    }
    result = {
        "ok": True,
        "status": "committed",
        "dry_run": False,
        "operation_id": operation_id,
        "plan_sha256": plan.plan_sha256,
        "disposed_work_count": sum(dispositions.values()),
        "deleted_rebuild_queue_count": len(plan.queue_rows),
        "deleted_reclassification_count": len(plan.reclassification_rows),
        "deleted_generated_edge_count": len(plan.generated_edges),
        "restored_scope_count": restored_scope_count,
        "dispositions": dispositions,
    }
    record_committed_operator_operation(
        conn,
        operation_id=operation_id,
        operation_kind=RELATION_CLEANUP_OPERATION_KIND,
        target_ref="legacy_relation_work",
        before=before,
        result=result,
        backup_path=backup_path,
        request_fingerprint=request_fingerprint,
        commit=False,
    )
    return result


def _existing_operation_result(
    row: dict[str, Any],
    *,
    request_fingerprint: str,
    replayed: bool,
) -> dict[str, Any]:
    if str(row.get("operation_kind") or "") != RELATION_CLEANUP_OPERATION_KIND:
        raise ValueError("operation_id already belongs to a different operation")
    if str(row.get("request_fingerprint") or "") != request_fingerprint:
        raise ValueError("operation_id request fingerprint conflict")
    try:
        result = json.loads(str(row.get("result_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("operator ledger result is corrupt") from exc
    if not isinstance(result, dict):
        raise ValueError("operator ledger result is not an object")
    result["replayed"] = bool(replayed)
    result["receipt_state"] = str(row.get("receipt_state") or "")
    result["receipt_path"] = str(row.get("receipt_path") or "")
    result["backup_path"] = str(row.get("backup_path") or "")
    return result


def run_legacy_relation_cleanup(
    db_path: Path,
    *,
    apply: bool = False,
    maintenance_confirmed: bool = False,
    scope_ids: Iterable[str],
    queue_statuses: Iterable[str] | None = None,
    expected_target_revision: int | None = None,
    terminal_state: str = "cancelled",
    expected_plan_sha256: str = "",
    operation_id: str = "",
    reason: str = "",
    max_rows: int = 10_000,
) -> dict[str, Any]:
    """Run a read-only plan or one fenced, verified, idempotent cleanup."""

    path = _verified_database_path(db_path)
    initial_identity = _database_file_identity(path)
    scope_values = tuple(scope_ids)
    status_values = tuple(queue_statuses) if queue_statuses is not None else None
    requested_fingerprint = ""
    if apply:
        if not maintenance_confirmed:
            raise ValueError("apply requires maintenance_confirmed")
        op_id = str(operation_id or "").strip()
        if not _SAFE_OPERATION_ID.fullmatch(op_id):
            raise ValueError("apply requires a safe stable operation_id")
        if len(str(reason or "").strip()) < 8:
            raise ValueError("apply requires a specific reason")
        expected_hash = str(expected_plan_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("apply requires an exact expected_plan_sha256")
        replay_scopes = tuple(
            sorted({str(value).strip() for value in scope_values if str(value).strip()})
        )
        replay_statuses = tuple(
            sorted(
                {
                    str(value).strip().lower()
                    for value in (status_values or _QUEUE_STATUSES)
                    if str(value).strip()
                }
            )
        )
        replay_terminal = str(terminal_state or "").strip().lower()
        replay_max_rows = max(1, min(int(max_rows), 100_000))
        requested_fingerprint = _cleanup_request_fingerprint_values(
            plan_sha256=expected_hash,
            scopes=replay_scopes,
            queue_statuses=replay_statuses,
            expected_target_revision=expected_target_revision,
            terminal_state=replay_terminal,
            max_rows=replay_max_rows,
            reason=reason,
        )
    if not apply:
        ro = connect_truth_database(path, mode="ro")
        try:
            plan = plan_legacy_relation_cleanup(
                ro,
                scope_ids=scope_values,
                queue_statuses=status_values,
                expected_target_revision=expected_target_revision,
                terminal_state=terminal_state,
                max_rows=max_rows,
            )
        finally:
            ro.close()
        return plan.public()
    fingerprint = requested_fingerprint

    lease: dict[str, Any] | None = None
    conn: sqlite3.Connection | None = None
    guards_installed = False
    committed = False
    backup_path = ""
    result: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    failure: BaseException | None = None
    post_commit_debt: list[str] = []
    receipt_error = ""
    replayed = False
    authoritative_row: dict[str, Any] | None = None
    try:
        lease = acquire_activation_lease(path)
        path = _revalidate_database_target(
            path,
            expected_identity=initial_identity,
        )
        conn = connect_truth_database(path, mode="rw")
        install_activation_lease_authorizer(
            conn,
            path,
            lease_token=str(lease["token"]),
        )
        conn.execute("BEGIN IMMEDIATE")
        path = _revalidate_database_target(
            path,
            expected_identity=initial_identity,
        )
        _verify_connection_target(conn, path)
        existing = read_operator_operation(conn, op_id)
        if existing is not None:
            result = _existing_operation_result(
                existing,
                request_fingerprint=fingerprint,
                replayed=True,
            )
            backup_path = str(existing.get("backup_path") or "")
            replayed = True
            committed = True
            conn.rollback()
        else:
            locked_plan = plan_legacy_relation_cleanup(
                conn,
                scope_ids=scope_values,
                queue_statuses=status_values,
                expected_target_revision=expected_target_revision,
                terminal_state=terminal_state,
                max_rows=max_rows,
            )
            if locked_plan.plan_sha256 != expected_hash:
                raise RuntimeError("cleanup plan changed under write fence")
            if locked_plan.truncated:
                raise RuntimeError("cleanup plan exceeds max_rows under write fence")
            backup_dir = _verified_backup_directory(path.parent / "backups")
            backup_dir.mkdir(exist_ok=True)
            backup_dir = _verified_backup_directory(backup_dir)
            operation_slug = _sha(op_id)[:16]
            backup_target = backup_dir / (
                f"relation-cleanup.{operation_slug}.{uuid.uuid4().hex}.sqlite3"
            )
            backup = verified_online_backup(path, backup_target)
            if Path(str(backup["backup_path"])) != backup_target:
                raise RuntimeError("cleanup backup helper returned an unexpected target")
            # Store a deterministic path relative to the truth directory.  It
            # remains useful on replay without leaking a machine-specific home
            # path into the authoritative ledger or receipt mirror.
            backup_path = (Path("backups") / backup_target.name).as_posix()
            ensure_activation_guard_triggers(
                conn,
                path,
                lease_token=str(lease["token"]),
            )
            guards_installed = True
            result = apply_legacy_relation_cleanup(
                conn,
                plan=locked_plan,
                operation_id=op_id,
                request_fingerprint=fingerprint,
                backup_path=backup_path,
            )
            conn.commit()
            committed = True
        try:
            receipt = mirror_operator_receipt(
                conn,
                db_path=path,
                operation_id=op_id,
            )
        except BaseException as exc:
            receipt_error = type(exc).__name__
        authoritative_row = read_operator_operation(conn, op_id)
        if authoritative_row is None:
            raise RuntimeError("committed cleanup operation is missing from its ledger")
    except BaseException as exc:
        if committed:
            post_commit_debt.append(f"post_commit_{type(exc).__name__}")
        else:
            failure = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
    finally:
        if conn is not None and guards_installed:
            try:
                remove_activation_guard_triggers(conn)
                conn.commit()
                guards_installed = False
            except BaseException as exc:
                if conn.in_transaction:
                    conn.rollback()
                if committed:
                    post_commit_debt.append(
                        f"activation_guard_cleanup_{type(exc).__name__}"
                    )
                elif failure is None:
                    failure = exc
        if conn is not None:
            try:
                conn.close()
            except BaseException as exc:
                if committed:
                    post_commit_debt.append(f"connection_close_{type(exc).__name__}")
                elif failure is None:
                    failure = exc
        released = lease is None
        if lease is not None and not guards_installed:
            try:
                released = release_activation_lease(lease)
            except BaseException as exc:
                released = False
                if committed:
                    post_commit_debt.append(
                        f"maintenance_lease_release_{type(exc).__name__}"
                    )
                elif failure is None:
                    failure = exc
        if lease is not None and guards_installed:
            post_commit_debt.append("activation_guards_and_lease_retained")
        elif lease is not None and not released:
            post_commit_debt.append("maintenance_lease_retained")

    if not committed:
        if failure is None and not released:
            failure = MaintenanceLeaseError(
                "relation cleanup maintenance lease release failed"
            )
        assert failure is not None
        raise RuntimeError("relation cleanup failed before commit") from failure
    if authoritative_row is not None:
        result = _existing_operation_result(
            authoritative_row,
            request_fingerprint=fingerprint,
            replayed=replayed,
        )
    assert result is not None
    if receipt is not None:
        result.update(receipt)
    receipt_state = str(
        (authoritative_row or {}).get("receipt_state")
        or result.get("receipt_state")
        or "pending"
    )
    result["receipt_state"] = receipt_state
    result["backup_path"] = str(
        (authoritative_row or {}).get("backup_path") or backup_path
    )
    result["maintenance_lease_released"] = bool(released)
    result["activation_guards_removed"] = not guards_installed
    if receipt_error:
        result["receipt_error"] = receipt_error
    if post_commit_debt:
        result["status"] = "committed_cleanup_debt"
        result["cleanup_debt"] = sorted(set(post_commit_debt))
    elif receipt_state != "mirrored":
        result["status"] = "committed_receipt_debt"
    else:
        result["status"] = "committed"
    return result


__all__ = [
    "LegacyRelationCleanupPlan",
    "RELATION_CLEANUP_OPERATION_KIND",
    "apply_legacy_relation_cleanup",
    "cleanup_request_fingerprint",
    "plan_legacy_relation_cleanup",
    "run_legacy_relation_cleanup",
]
