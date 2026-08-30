"""Explicit deny-first, two-phase privacy purge maintenance service."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, cast

from .capture import capture_mutation_barrier
from .capture_filters import sanitize_report_text
from .fact_repository import FACT_EXECUTOR_MUTATION_AUTHORITY
from .graph import load_metadata
from .lifecycle_registry import PRIVACY_PURGE_DENY
from .lifecycle_service import transition_memory_lifecycle
from .memory_mutation import MemoryMutationService
from .operator_ledger import (
    mirror_operator_receipt,
    record_committed_operator_operation,
)
from .privacy_purge_schema import ensure_privacy_purge_schema
from .response_schemas import retention_response_contract
from .sql_store import delete_rows, record_governance_audit_event
from .vector_runtime import replay_vector_outbox
from .windows_filesystem import list_directory_paths, path_is_dir, read_text

_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PrivacyPurgeError(RuntimeError):
    """A public purge request failed closed before weakening deny state."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _target_hash(scope_id: str, memory_id: str) -> str:
    return _sha256(f"scope-recall/privacy-purge/target/v1\0{scope_id}\0{memory_id}")


def _source_hash(journal_entry_id: int) -> str:
    return _sha256(
        f"scope-recall/privacy-purge/journal-source/v1\0{int(journal_entry_id)}"
    )


def _clean_operation_id(operation_id: str, *, generate: bool) -> str:
    cleaned = str(operation_id or "").strip()
    if not cleaned and generate:
        cleaned = f"purge_{uuid.uuid4().hex}"
    if not _SAFE_OPERATION_ID.fullmatch(cleaned):
        raise PrivacyPurgeError(
            "operation_id must contain 1-96 letters, digits, dot, underscore, or dash"
        )
    return cleaned


def _clean_ids(ids: Iterable[str]) -> list[str]:
    return list(
        dict.fromkeys(str(memory_id).strip() for memory_id in ids if str(memory_id).strip())
    )


def _provider_lock(provider: Any) -> Any:
    fn = getattr(provider, "query_lock", None)
    if not callable(fn):
        raise PrivacyPurgeError("query lock is unavailable")
    return fn()


def _provider_conn(provider: Any) -> sqlite3.Connection:
    fn = getattr(provider, "query_connection", None)
    if not callable(fn):
        raise PrivacyPurgeError("truth connection is unavailable")
    return cast(sqlite3.Connection, fn())


def _writable_scope_ids(provider: Any) -> list[str]:
    fn = getattr(provider, "writable_scope_ids", None)
    if not callable(fn):
        raise PrivacyPurgeError("writable scope authority is unavailable")
    values = fn()
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise PrivacyPurgeError("writable scope authority returned an invalid value")
    return sorted({str(value) for value in values if str(value)})


def _db_path(provider: Any) -> Path | None:
    raw = getattr(provider, "_db_path", None)
    if raw:
        return Path(raw)
    fn = getattr(provider, "runtime_status_view", None)
    if callable(fn):
        status = fn()
        if isinstance(status, Mapping) and status.get("db_path"):
            return Path(str(status["db_path"]))
    return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _rows_for_request(
    conn: sqlite3.Connection,
    *,
    ids: Sequence[str],
    scope_ids: Sequence[str],
) -> list[sqlite3.Row]:
    if not ids or not scope_ids:
        return []
    id_marks = ",".join("?" for _ in ids)
    scope_marks = ",".join("?" for _ in scope_ids)
    return list(
        conn.execute(
            f"""
            SELECT id, scope_id, content, updated_at, metadata
            FROM memories
            WHERE id IN ({id_marks}) AND scope_id IN ({scope_marks})
            ORDER BY scope_id, id
            """,
            [*ids, *scope_ids],
        ).fetchall()
    )


def _request_material(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
    targets = [
        {
            "target_hash": _target_hash(str(row["scope_id"]), str(row["id"])),
            "content_hash": _sha256(str(row["content"] or "")),
            "state_hash": _sha256(
                f"{str(row['updated_at'] or '')}\0{_sha256(str(row['metadata'] or '{}'))}"
            ),
        }
        for row in rows
    ]
    targets.sort(key=lambda item: item["target_hash"])
    scope_hashes = sorted({_sha256(str(row["scope_id"])) for row in rows})
    target_hashes = [str(item["target_hash"]) for item in targets]
    fingerprint = _sha256(_canonical_json(targets))
    return {
        "targets": targets,
        "target_hashes": target_hashes,
        "scope_hashes": scope_hashes,
        "scope_set_hash": _sha256(_canonical_json(scope_hashes)),
        "request_fingerprint": fingerprint,
    }


def _deny_confirmation(operation_id: str, fingerprint: str) -> str:
    digest = _sha256(f"privacy-purge/v2/deny\0{operation_id}\0{fingerprint}")
    return f"DENY-{digest[:32]}"


def _erase_confirmation(operation_id: str, fingerprint: str) -> str:
    digest = _sha256(f"privacy-purge/v2/erase\0{operation_id}\0{fingerprint}")
    return f"ERASE-{digest[:32]}"


def _operation_row(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row | None:
    if not _table_exists(conn, "privacy_purge_operations"):
        return None
    return conn.execute(
        "SELECT * FROM privacy_purge_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()


def _source_rows(conn: sqlite3.Connection, memory_ids: Sequence[str]) -> list[sqlite3.Row]:
    if not memory_ids or not _table_exists(conn, "memory_journal_sources"):
        return []
    marks = ",".join("?" for _ in memory_ids)
    return list(
        conn.execute(
            f"""
            SELECT DISTINCT journal_entry_id
            FROM memory_journal_sources
            WHERE memory_id IN ({marks})
            ORDER BY journal_entry_id
            """,
            list(memory_ids),
        ).fetchall()
    )


def plan_privacy_purge(
    provider: Any,
    *,
    ids: Sequence[str],
    operation_id: str = "",
) -> dict[str, Any]:
    """Return a zero-write, content-free confirmation plan."""

    requested = _clean_ids(ids)
    op_id = _clean_operation_id(operation_id, generate=True)
    if not requested:
        raise PrivacyPurgeError("exact memory ids are required")
    with _provider_lock(provider):
        conn = _provider_conn(provider)
        rows = _rows_for_request(
            conn, ids=requested, scope_ids=_writable_scope_ids(provider)
        )
        material = _request_material(rows)
    if len(rows) != len(requested):
        raise PrivacyPurgeError(
            "every requested id must exist exactly once in the current writable scope set"
        )
    return {
        "ok": True,
        "action": "plan",
        "read_only": True,
        "operation_id": op_id,
        "target_count": len(rows),
        "scope_set_hash": material["scope_set_hash"],
        "request_fingerprint": material["request_fingerprint"],
        "target_hashes": material["target_hashes"],
        "confirmation": _deny_confirmation(
            op_id, str(material["request_fingerprint"])
        ),
        "next_action": "deny",
        **retention_response_contract(
            mode="privacy_purge",
            data_retained=True,
            mutation_applied=False,
        ),
    }


def privacy_purge_status(provider: Any, *, operation_id: str) -> dict[str, Any]:
    op_id = _clean_operation_id(operation_id, generate=False)
    with _provider_lock(provider):
        conn = _provider_conn(provider)
        row = _operation_row(conn, op_id)
        if row is None:
            return {
                "ok": False,
                "found": False,
                "operation_id": op_id,
                **retention_response_contract(
                    mode="privacy_purge",
                    data_retained=True,
                    mutation_applied=False,
                ),
            }
        target_hashes = [
            str(item[0])
            for item in conn.execute(
                "SELECT target_hash FROM privacy_purge_tombstones "
                "WHERE operation_id=? ORDER BY target_hash",
                (op_id,),
            ).fetchall()
        ]
        pending_vector_intents = int(
            conn.execute(
                "SELECT COUNT(*) FROM privacy_purge_vector_intents "
                "WHERE operation_id=? AND completed=0",
                (op_id,),
            ).fetchone()[0]
        )
    status = str(row["status"])
    return {
        "ok": True,
        "found": True,
        "operation_id": op_id,
        "status": status,
        "target_count": int(row["target_count"]),
        "source_count": int(row["source_count"]),
        "vector_intent_count": int(row["vector_intent_count"]),
        "scope_set_hash": str(row["scope_set_hash"]),
        "request_fingerprint": str(row["request_fingerprint"]),
        "target_hashes": target_hashes,
        "pending_vector_intents": pending_vector_intents,
        "erase_confirmation": _erase_confirmation(
            op_id, str(row["request_fingerprint"])
        ),
        **retention_response_contract(
            mode="privacy_purge",
            data_retained=status == "denied",
            mutation_applied=False,
            companion_erasure_pending=pending_vector_intents > 0,
        ),
    }


def _mirror_receipt(provider: Any, ledger_operation_id: str) -> dict[str, Any]:
    db_path = _db_path(provider)
    if db_path is None:
        return {"receipt_state": "pending", "error": "database path unavailable"}
    try:
        with _provider_lock(provider):
            return mirror_operator_receipt(
                _provider_conn(provider),
                db_path=db_path,
                operation_id=ledger_operation_id,
            )
    except Exception as exc:
        return {
            "receipt_state": "pending",
            "error": sanitize_report_text(str(exc))[:300],
        }


def _replay_vector(provider: Any) -> dict[str, Any]:
    try:
        return dict(replay_vector_outbox(provider))
    except Exception as exc:
        return {
            "claimed": 0,
            "completed": 0,
            "failed": 1,
            "pending": True,
            "error": sanitize_report_text(str(exc))[:300],
        }


def deny_privacy_purge(
    provider: Any,
    *,
    ids: Sequence[str],
    operation_id: str,
    confirmation: str,
) -> dict[str, Any]:
    """Commit Phase A deny before attempting any physical erasure."""

    requested = _clean_ids(ids)
    op_id = _clean_operation_id(operation_id, generate=False)
    if not requested:
        raise PrivacyPurgeError("exact memory ids are required")
    with capture_mutation_barrier(provider):
        with MemoryMutationService(provider).transaction() as conn:
            ensure_privacy_purge_schema(conn)
            existing = _operation_row(conn, op_id)
            rows = _rows_for_request(
                conn, ids=requested, scope_ids=_writable_scope_ids(provider)
            )
            material = _request_material(rows)
            if len(rows) != len(requested):
                raise PrivacyPurgeError(
                    "every requested id must exist exactly once in the current writable scope set"
                )
            expected_confirmation = _deny_confirmation(
                op_id, str(material["request_fingerprint"])
            )
            if str(confirmation or "") != expected_confirmation:
                raise PrivacyPurgeError(
                    "confirmation does not match the exact operation and target set"
                )
            if existing is not None:
                if str(existing["request_fingerprint"]) != str(
                    material["request_fingerprint"]
                ):
                    raise PrivacyPurgeError(
                        "operation_id already belongs to a different target set"
                    )
                MemoryMutationService.abort(conn)
                return privacy_purge_status(provider, operation_id=op_id)

            now = _now_iso()
            source_rows = _source_rows(conn, [str(row["id"]) for row in rows])
            vector_intents: list[tuple[str, str]] = []
            targets_by_hash = {
                str(target["target_hash"]): target for target in material["targets"]
            }
            for row in rows:
                memory_id = str(row["id"])
                target_hash = _target_hash(str(row["scope_id"]), memory_id)
                target = targets_by_hash[target_hash]
                result = transition_memory_lifecycle(
                    conn,
                    memory_id=memory_id,
                    lifecycle="archived",
                    metadata_updates={
                        "purge_denied": True,
                        "purge_operation_id": op_id,
                        "purge_target_hash": target_hash,
                        "purge_denied_at": now,
                    },
                    expected_updated_at=str(row["updated_at"] or ""),
                    actor="scope-recall:privacy-purge",
                    reason="privacy purge deny-first",
                    operation_id=PRIVACY_PURGE_DENY,
                    batch_id=op_id,
                    timestamp=now,
                    fact_mutation_authority=FACT_EXECUTOR_MUTATION_AUTHORITY,
                    audit_content_free=True,
                    audit_target_id=target_hash,
                )
                event_key = str(result.get("vector_outbox_key") or "")
                if event_key:
                    vector_intents.append((event_key, target_hash))
                conn.execute(
                    """
                    INSERT INTO privacy_purge_tombstones(
                        operation_id, target_hash, content_hash, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (op_id, target_hash, str(target["content_hash"]), now),
                )
            source_hashes: list[str] = []
            for source_row in source_rows:
                entry_id = int(source_row["journal_entry_id"])
                source_hash = _source_hash(entry_id)
                source_hashes.append(source_hash)
                conn.execute(
                    """
                    INSERT INTO privacy_purge_source_tombstones(
                        operation_id, journal_entry_id, source_hash, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (op_id, entry_id, source_hash, now),
                )
            for event_key, target_hash in vector_intents:
                conn.execute(
                    """
                    INSERT INTO privacy_purge_vector_intents(
                        operation_id, event_key, target_hash, completed,
                        completed_at, created_at
                    ) VALUES (?, ?, ?, 0, '', ?)
                    """,
                    (op_id, event_key, target_hash, now),
                )
            conn.execute(
                """
                INSERT INTO privacy_purge_operations(
                    operation_id, request_fingerprint, scope_set_hash,
                    target_count, source_count, vector_intent_count, status,
                    created_at, updated_at, denied_at, erased_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'denied', ?, ?, ?, '')
                """,
                (
                    op_id,
                    material["request_fingerprint"],
                    material["scope_set_hash"],
                    len(rows),
                    len(source_rows),
                    len(vector_intents),
                    now,
                    now,
                    now,
                ),
            )
            ledger_result = {
                "operation_id": op_id,
                "phase": "deny",
                "status": "denied",
                "target_count": len(rows),
                "source_count": len(source_rows),
                "vector_intent_count": len(vector_intents),
                "scope_set_hash": material["scope_set_hash"],
                "scope_hashes": material["scope_hashes"],
                "request_fingerprint": material["request_fingerprint"],
                "targets": material["targets"],
                "source_hashes": sorted(source_hashes),
                "denied_at": now,
            }
            record_committed_operator_operation(
                conn,
                operation_id=f"{op_id}.deny",
                operation_kind="privacy_purge.deny",
                target_ref=str(material["scope_set_hash"]),
                before={"target_count": len(rows)},
                result=ledger_result,
                backup_path="",
                request_fingerprint=str(material["request_fingerprint"]),
                commit=False,
            )

    receipt_mirror = _mirror_receipt(provider, f"{op_id}.deny")
    vector_replay = _replay_vector(provider)
    _sync_vector_intents(provider, op_id)
    current_status = privacy_purge_status(provider, operation_id=op_id)
    return {
        "ok": True,
        "action": "deny",
        "operation_id": op_id,
        "status": "denied",
        "target_count": len(requested),
        "scope_set_hash": material["scope_set_hash"],
        "request_fingerprint": material["request_fingerprint"],
        "target_hashes": material["target_hashes"],
        "erase_confirmation": _erase_confirmation(
            op_id, str(material["request_fingerprint"])
        ),
        "receipt": receipt_mirror,
        "vector_replay": vector_replay,
        **retention_response_contract(
            mode="privacy_purge",
            data_retained=True,
            mutation_applied=True,
            companion_erasure_pending=bool(
                current_status.get("companion_erasure_pending")
            ),
        ),
        "next_action": "erase",
    }


def _decode_json_object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _source_ref_values(entry_id: int) -> tuple[str, ...]:
    raw = str(int(entry_id))
    return (raw, f"message:{raw}", f"journal:{raw}", f"journal_entry:{raw}")


def _validated_spans(
    conn: sqlite3.Connection,
    *,
    memory_ids: Sequence[str],
    journal_entry_id: int,
    content: str,
) -> list[tuple[int, int]]:
    if not memory_ids or not _table_exists(conn, "fact_claim_evidence"):
        return []
    memory_marks = ",".join("?" for _ in memory_ids)
    refs = _source_ref_values(journal_entry_id)
    ref_marks = ",".join("?" for _ in refs)
    rows = conn.execute(
        f"""
        SELECT fce.metadata
        FROM fact_claim_evidence AS fce
        JOIN fact_claims AS fc ON fc.claim_id=fce.claim_id
        WHERE fc.memory_id IN ({memory_marks}) AND fce.source_ref IN ({ref_marks})
        """,
        [*memory_ids, *refs],
    ).fetchall()
    if not rows:
        return []
    spans: list[tuple[int, int]] = []
    for row in rows:
        metadata = _decode_json_object(row["metadata"])
        start = metadata.get("span_start", metadata.get("start"))
        end = metadata.get("span_end", metadata.get("end"))
        digest = str(metadata.get("span_sha256") or "").lower()
        if not isinstance(start, int) or not isinstance(end, int):
            return []
        if start < 0 or end <= start or end > len(content) or not _SHA256.fullmatch(digest):
            return []
        if _sha256(content[start:end]) != digest:
            return []
        spans.append((start, end))
    ordered = sorted(set(spans))
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        return []
    return ordered


def _redact_journal_sources(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    memory_ids: Sequence[str],
    timestamp: str,
) -> dict[str, int]:
    counts = {"span_redacted": 0, "body_removed": 0, "provenance_degraded": 0}
    if not _table_exists(conn, "journal_entries"):
        return counts
    source_rows = conn.execute(
        "SELECT journal_entry_id FROM privacy_purge_source_tombstones "
        "WHERE operation_id=? ORDER BY journal_entry_id",
        (operation_id,),
    ).fetchall()
    memory_set = set(memory_ids)
    for source_row in source_rows:
        entry_id = int(source_row["journal_entry_id"])
        entry = conn.execute(
            "SELECT content, content_hash, metadata FROM journal_entries WHERE id=?",
            (entry_id,),
        ).fetchone()
        if entry is None:
            continue
        content = str(entry["content"] or "")
        metadata = _decode_json_object(entry["metadata"])
        metadata["privacy_purge_operation_hash"] = _sha256(operation_id)
        metadata["privacy_purged_at"] = timestamp
        spans = _validated_spans(
            conn,
            memory_ids=memory_ids,
            journal_entry_id=entry_id,
            content=content,
        )
        if spans:
            redacted = content
            for start, end in reversed(spans):
                redacted = redacted[:start] + "[PURGED]" + redacted[end:]
            metadata["privacy_purge_redaction"] = "reliable_span"
            conn.execute(
                "UPDATE journal_entries SET content=?, content_hash=?, metadata=? WHERE id=?",
                (
                    redacted,
                    _sha256(redacted),
                    _canonical_json(metadata),
                    entry_id,
                ),
            )
            counts["span_redacted"] += 1
        else:
            metadata["privacy_purge_redaction"] = "body_removed"
            metadata["pre_purge_content_hash"] = str(entry["content_hash"] or "")
            conn.execute(
                "UPDATE journal_entries SET content='', metadata=? WHERE id=?",
                (_canonical_json(metadata), entry_id),
            )
            counts["body_removed"] += 1

        other_memories = []
        if _table_exists(conn, "memory_journal_sources"):
            other_memories = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT memory_id FROM memory_journal_sources "
                    "WHERE journal_entry_id=? ORDER BY memory_id",
                    (entry_id,),
                ).fetchall()
                if str(row[0]) not in memory_set
            ]
        for other_id in other_memories:
            row = conn.execute(
                "SELECT metadata FROM memories WHERE id=?", (other_id,)
            ).fetchone()
            if row is None:
                continue
            other_metadata = load_metadata(row["metadata"])
            other_metadata["provenance_degraded"] = True
            other_metadata["provenance_degraded_at"] = timestamp
            conn.execute(
                "UPDATE memories SET metadata=? WHERE id=?",
                (_canonical_json(other_metadata), other_id),
            )
            counts["provenance_degraded"] += 1

        if _table_exists(conn, "fact_claim_evidence"):
            refs = _source_ref_values(entry_id)
            marks = ",".join("?" for _ in refs)
            evidence_rows = conn.execute(
                f"SELECT evidence_id, claim_id, metadata FROM fact_claim_evidence "
                f"WHERE source_ref IN ({marks})",
                list(refs),
            ).fetchall()
            affected_claims: set[str] = set()
            for evidence in evidence_rows:
                evidence_metadata = _decode_json_object(evidence["metadata"])
                evidence_metadata["provenance_degraded"] = True
                evidence_metadata["privacy_purge_operation_hash"] = _sha256(operation_id)
                conn.execute(
                    "UPDATE fact_claim_evidence SET excerpt='', metadata=? WHERE evidence_id=?",
                    (_canonical_json(evidence_metadata), str(evidence["evidence_id"])),
                )
                affected_claims.add(str(evidence["claim_id"]))
            for claim_id in sorted(affected_claims):
                claim = conn.execute(
                    "SELECT metadata FROM fact_claims WHERE claim_id=?", (claim_id,)
                ).fetchone()
                if claim is None:
                    continue
                claim_metadata = _decode_json_object(claim["metadata"])
                claim_metadata["provenance_degraded"] = True
                claim_metadata["provenance_degraded_at"] = timestamp
                conn.execute(
                    "UPDATE fact_claims SET metadata=? WHERE claim_id=?",
                    (_canonical_json(claim_metadata), claim_id),
                )
    return counts


def _vector_intents_complete(conn: sqlite3.Connection, operation_id: str) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN pvi.completed=1 OR vo.status='completed' THEN 1 ELSE 0 END) AS completed
        FROM privacy_purge_vector_intents AS pvi
        LEFT JOIN vector_outbox AS vo ON vo.event_key=pvi.event_key
        WHERE pvi.operation_id=?
        """,
        (operation_id,),
    ).fetchone()
    total = int(row["total"] or 0)
    completed = int(row["completed"] or 0)
    return total == completed


def _sync_vector_intents(provider: Any, operation_id: str) -> None:
    with _provider_lock(provider):
        conn = _provider_conn(provider)
        conn.execute(
            """
            UPDATE privacy_purge_vector_intents
            SET completed=1, completed_at=?
            WHERE operation_id=? AND completed=0 AND event_key IN (
                SELECT event_key FROM vector_outbox WHERE status='completed'
            )
            """,
            (_now_iso(), operation_id),
        )
        conn.commit()


def _finalize_vector_status(provider: Any, operation_id: str) -> str:
    with _provider_lock(provider):
        conn = _provider_conn(provider)
        if _vector_intents_complete(conn, operation_id):
            conn.execute(
                "UPDATE privacy_purge_operations SET status='completed', updated_at=? "
                "WHERE operation_id=? AND status='erasure_pending_vector'",
                (_now_iso(), operation_id),
            )
            conn.commit()
            return "completed"
        return "erasure_pending_vector"


def erase_privacy_purge(
    provider: Any,
    *,
    operation_id: str,
    confirmation: str,
) -> dict[str, Any]:
    """Run idempotent Phase B erasure while preserving deny tombstones."""

    op_id = _clean_operation_id(operation_id, generate=False)
    with _provider_lock(provider):
        initial = _operation_row(_provider_conn(provider), op_id)
    if initial is None:
        raise PrivacyPurgeError("unknown purge operation")
    fingerprint = str(initial["request_fingerprint"])
    if str(confirmation or "") != _erase_confirmation(op_id, fingerprint):
        raise PrivacyPurgeError("erase confirmation does not match the denied operation")
    if str(initial["status"]) == "completed":
        return privacy_purge_status(provider, operation_id=op_id)

    phase_b_applied = str(initial["status"]) == "denied"

    if str(initial["status"]) == "denied":
        with capture_mutation_barrier(provider):
            with MemoryMutationService(provider).transaction() as conn:
                current = _operation_row(conn, op_id)
                if current is None:
                    raise PrivacyPurgeError("unknown purge operation")
                rows = conn.execute(
                    """
                    SELECT id, scope_id, metadata
                    FROM memories
                    WHERE json_valid(metadata)
                      AND json_extract(metadata, '$.purge_operation_id')=?
                      AND json_extract(metadata, '$.purge_denied')=1
                    ORDER BY scope_id, id
                    """,
                    (op_id,),
                ).fetchall()
                writable = set(_writable_scope_ids(provider))
                if any(str(row["scope_id"]) not in writable for row in rows):
                    raise PrivacyPurgeError(
                        "current writable authority no longer covers every denied target"
                    )
                memory_ids = [str(row["id"]) for row in rows]
                expected_count = int(current["target_count"])
                tombstone_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM privacy_purge_tombstones "
                        "WHERE operation_id=?",
                        (op_id,),
                    ).fetchone()[0]
                )
                if tombstone_count != expected_count:
                    raise PrivacyPurgeError(
                        "denied tombstone set is incomplete; replay the purge ledger before erasure"
                    )
                erased_at = _now_iso()
                provenance_counts = _redact_journal_sources(
                    conn,
                    operation_id=op_id,
                    memory_ids=memory_ids,
                    timestamp=erased_at,
                )
                claim_count = 0
                evidence_count = 0
                if memory_ids and _table_exists(conn, "fact_claims"):
                    marks = ",".join("?" for _ in memory_ids)
                    claim_ids = [
                        str(row[0])
                        for row in conn.execute(
                            f"SELECT claim_id FROM fact_claims WHERE memory_id IN ({marks})",
                            memory_ids,
                        ).fetchall()
                    ]
                    claim_count = len(claim_ids)
                    if claim_ids and _table_exists(conn, "fact_claim_evidence"):
                        claim_marks = ",".join("?" for _ in claim_ids)
                        evidence_count = int(
                            conn.execute(
                                f"SELECT COUNT(*) FROM fact_claim_evidence "
                                f"WHERE claim_id IN ({claim_marks})",
                                claim_ids,
                            ).fetchone()[0]
                        )
                    conn.execute(
                        f"DELETE FROM fact_claims WHERE memory_id IN ({marks})", memory_ids
                    )
                deleted = delete_rows(
                    conn,
                    memory_ids,
                    scope_ids=sorted(writable),
                    commit=False,
                )
                if deleted != len(memory_ids):
                    raise PrivacyPurgeError("physical Projection erasure was incomplete")
                audit_event_id = f"privacy_purge_{uuid.uuid4().hex}"
                record_governance_audit_event(
                    conn,
                    event_id=audit_event_id,
                    event_type="privacy_purge",
                    action="physical_erase",
                    batch_id=op_id,
                    before={
                        "target_count": expected_count,
                        "request_fingerprint": fingerprint,
                    },
                    after={
                        "projection_deleted": deleted,
                        "claim_deleted": claim_count,
                        "evidence_deleted": evidence_count,
                        **provenance_counts,
                    },
                    reason="confirmed privacy purge physical erasure",
                    actor="scope-recall:privacy-purge",
                    dry_run=False,
                    created_at=erased_at,
                )
                conn.execute(
                    """
                    UPDATE privacy_purge_operations
                    SET status='erasure_pending_vector', updated_at=?, erased_at=?
                    WHERE operation_id=? AND status='denied'
                    """,
                    (erased_at, erased_at, op_id),
                )
                ledger_result = {
                    "operation_id": op_id,
                    "phase": "erase",
                    "status": "erasure_pending_vector",
                    "target_count": expected_count,
                    "projection_deleted": deleted,
                    "claim_deleted": claim_count,
                    "evidence_deleted": evidence_count,
                    "scope_set_hash": str(current["scope_set_hash"]),
                    "request_fingerprint": fingerprint,
                    "erased_at": erased_at,
                    **provenance_counts,
                }
                record_committed_operator_operation(
                    conn,
                    operation_id=f"{op_id}.erase",
                    operation_kind="privacy_purge.erase",
                    target_ref=str(current["scope_set_hash"]),
                    before={"target_count": expected_count, "status": "denied"},
                    result=ledger_result,
                    backup_path="",
                    request_fingerprint=fingerprint,
                    commit=False,
                )

    receipt_mirror = _mirror_receipt(provider, f"{op_id}.erase")
    vector_replay = _replay_vector(provider)
    _sync_vector_intents(provider, op_id)
    status = _finalize_vector_status(provider, op_id)
    payload = privacy_purge_status(provider, operation_id=op_id)
    payload.update(
        {
            "action": "erase",
            "status": status,
            "receipt": receipt_mirror,
            "vector_replay": vector_replay,
            **retention_response_contract(
                mode="privacy_purge",
                data_retained=False,
                mutation_applied=phase_b_applied,
                companion_erasure_pending=status == "erasure_pending_vector",
            ),
        }
    )
    return payload


def run_privacy_purge(
    provider: Any,
    *,
    action: str,
    ids: Sequence[str] = (),
    operation_id: str = "",
    confirmation: str = "",
) -> dict[str, Any]:
    normalized = str(action or "").strip().lower().replace("-", "_")
    if normalized == "plan":
        return plan_privacy_purge(provider, ids=ids, operation_id=operation_id)
    if normalized == "status":
        return privacy_purge_status(provider, operation_id=operation_id)
    if normalized == "deny":
        return deny_privacy_purge(
            provider,
            ids=ids,
            operation_id=operation_id,
            confirmation=confirmation,
        )
    if normalized == "erase":
        try:
            return erase_privacy_purge(
                provider, operation_id=operation_id, confirmation=confirmation
            )
        except Exception as exc:
            current = privacy_purge_status(provider, operation_id=operation_id)
            if not bool(current.get("found")):
                raise
            current.update(
                {
                    "ok": False,
                    "action": "erase",
                    "error": sanitize_report_text(str(exc))[:300],
                }
            )
            return current
    raise PrivacyPurgeError("action must be one of: plan, status, deny, erase")


def _validated_deny_receipt(path: Path, payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    if (
        payload.get("schema_version") != "operator_receipt.v1"
        or payload.get("receipt_state") != "mirrored"
        or payload.get("operation_kind") != "privacy_purge.deny"
        or payload.get("action") != "deny"
    ):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    try:
        op_id = _clean_operation_id(
            str(result.get("operation_id") or ""), generate=False
        )
    except PrivacyPurgeError:
        return None
    ledger_operation_id = f"{op_id}.deny"
    if str(payload.get("operation_id") or "") != ledger_operation_id:
        return None
    if path.name != f"operator.deny.{ledger_operation_id}.json":
        return None
    if result.get("phase") != "deny" or result.get("status") != "denied":
        return None
    fingerprint = str(result.get("request_fingerprint") or "")
    scope_set_hash = str(result.get("scope_set_hash") or "")
    if (
        not _SHA256.fullmatch(fingerprint)
        or not _SHA256.fullmatch(scope_set_hash)
        or str(payload.get("request_fingerprint") or "") != fingerprint
        or str(payload.get("target_ref") or "") != scope_set_hash
    ):
        return None
    raw_targets = result.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        return None
    targets: list[dict[str, str]] = []
    for item in raw_targets:
        if not isinstance(item, Mapping):
            return None
        target = {
            "target_hash": str(item.get("target_hash") or ""),
            "content_hash": str(item.get("content_hash") or ""),
            "state_hash": str(item.get("state_hash") or ""),
        }
        if any(not _SHA256.fullmatch(value) for value in target.values()):
            return None
        targets.append(target)
    targets.sort(key=lambda item: item["target_hash"])
    if len({item["target_hash"] for item in targets}) != len(targets):
        return None
    try:
        target_count = int(result.get("target_count") or -1)
        source_count = int(result.get("source_count") or 0)
    except (TypeError, ValueError):
        return None
    if target_count != len(targets):
        return None
    if _sha256(_canonical_json(targets)) != fingerprint:
        return None
    raw_scope_hashes = result.get("scope_hashes")
    if not isinstance(raw_scope_hashes, list):
        return None
    scope_hashes = sorted({str(value) for value in raw_scope_hashes})
    if (
        not scope_hashes
        or any(not _SHA256.fullmatch(value) for value in scope_hashes)
        or _sha256(_canonical_json(scope_hashes)) != scope_set_hash
    ):
        return None
    raw_source_hashes = result.get("source_hashes", [])
    if not isinstance(raw_source_hashes, list):
        return None
    source_hashes = sorted({str(value) for value in raw_source_hashes})
    if any(not _SHA256.fullmatch(value) for value in source_hashes):
        return None
    if source_count != len(source_hashes):
        return None
    return {
        "operation_id": op_id,
        "request_fingerprint": fingerprint,
        "scope_set_hash": scope_set_hash,
        "targets": targets,
        "source_hashes": source_hashes,
    }


def replay_privacy_purge_receipts(
    conn: sqlite3.Connection,
    *,
    receipt_dir: Path,
    commit: bool = True,
) -> dict[str, Any]:
    """Replay immutable content-free deny receipts into a restored backup."""

    ensure_privacy_purge_schema(conn)
    receipt_paths = (
        [
            path
            for path in list_directory_paths(receipt_dir)
            if path.match("operator.deny.*.json")
        ]
        if path_is_dir(receipt_dir)
        else []
    )
    receipts: list[dict[str, Any]] = []
    invalid_receipts = 0
    target_owners: dict[str, str] = {}
    for path in receipt_paths:
        try:
            payload = json.loads(read_text(path, encoding="utf-8"))
        except (OSError, ValueError):
            invalid_receipts += 1
            continue
        receipt = _validated_deny_receipt(path, payload)
        if receipt is None:
            invalid_receipts += 1
            continue
        op_id = str(receipt["operation_id"])
        target_hashes = [str(item["target_hash"]) for item in receipt["targets"]]
        if any(
            target_hash in target_owners and target_owners[target_hash] != op_id
            for target_hash in target_hashes
        ):
            invalid_receipts += 1
            continue
        for target_hash in target_hashes:
            target_owners[target_hash] = op_id
        receipts.append(receipt)

    rows = conn.execute(
        "SELECT id, scope_id, content, updated_at, metadata FROM memories ORDER BY scope_id, id"
    ).fetchall()
    source_ids = (
        [int(row[0]) for row in conn.execute("SELECT id FROM journal_entries").fetchall()]
        if _table_exists(conn, "journal_entries")
        else []
    )
    denied = 0
    operations = 0
    for receipt in receipts:
        op_id = str(receipt["operation_id"])
        fingerprint = str(receipt.get("request_fingerprint") or "")
        scope_set_hash = str(receipt.get("scope_set_hash") or "")
        targets = receipt.get("targets")
        assert isinstance(targets, list)
        target_map = {
            str(item.get("target_hash") or ""): str(item.get("content_hash") or "")
            for item in targets
            if isinstance(item, Mapping)
        }
        now = _now_iso()
        matched = [
            row
            for row in rows
            if _target_hash(str(row["scope_id"]), str(row["id"])) in target_map
        ]
        source_hashes = {str(value) for value in receipt.get("source_hashes", [])}
        matched_sources = [entry_id for entry_id in source_ids if _source_hash(entry_id) in source_hashes]
        existing = _operation_row(conn, op_id)
        if existing is not None and str(existing["request_fingerprint"]) != fingerprint:
            invalid_receipts += 1
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO privacy_purge_operations(
                operation_id, request_fingerprint, scope_set_hash,
                target_count, source_count, vector_intent_count, status,
                created_at, updated_at, denied_at, erased_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'denied', ?, ?, ?, '')
            """,
            (op_id, fingerprint, scope_set_hash, len(target_map), len(matched_sources), now, now, now),
        )
        for target_hash, content_hash in sorted(target_map.items()):
            conn.execute(
                "INSERT OR IGNORE INTO privacy_purge_tombstones("
                "operation_id,target_hash,content_hash,created_at) VALUES(?,?,?,?)",
                (op_id, target_hash, content_hash, now),
            )
        for row in matched:
            target_hash = _target_hash(str(row["scope_id"]), str(row["id"]))
            metadata = load_metadata(row["metadata"])
            if not bool(metadata.get("purge_denied")):
                transition_result = transition_memory_lifecycle(
                    conn,
                    memory_id=str(row["id"]),
                    lifecycle="archived",
                    metadata_updates={
                        "purge_denied": True,
                        "purge_operation_id": op_id,
                        "purge_target_hash": target_hash,
                        "purge_denied_at": now,
                    },
                    expected_updated_at=str(row["updated_at"] or ""),
                    actor="scope-recall:purge-ledger-replay",
                    reason="restore-time privacy purge deny replay",
                    operation_id=PRIVACY_PURGE_DENY,
                    batch_id=op_id,
                    timestamp=now,
                    fact_mutation_authority=FACT_EXECUTOR_MUTATION_AUTHORITY,
                    audit_content_free=True,
                    audit_target_id=target_hash,
                )
                event_key = str(transition_result.get("vector_outbox_key") or "")
                if event_key:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO privacy_purge_vector_intents(
                            operation_id, event_key, target_hash, completed,
                            completed_at, created_at
                        ) VALUES (?, ?, ?, 0, '', ?)
                        """,
                        (op_id, event_key, target_hash, now),
                    )
                denied += 1
        for entry_id in matched_sources:
            conn.execute(
                "INSERT OR IGNORE INTO privacy_purge_source_tombstones(" 
                "operation_id,journal_entry_id,source_hash,created_at) VALUES(?,?,?,?)",
                (op_id, entry_id, _source_hash(entry_id), now),
            )
        operations += 1
    if commit:
        conn.commit()
    return {
        "ok": True,
        "receipt_count": len(receipts),
        "invalid_receipt_count": invalid_receipts,
        "operations_replayed": operations,
        "targets_denied": denied,
    }


__all__ = [
    "PrivacyPurgeError",
    "deny_privacy_purge",
    "erase_privacy_purge",
    "plan_privacy_purge",
    "privacy_purge_status",
    "replay_privacy_purge_receipts",
    "run_privacy_purge",
]
