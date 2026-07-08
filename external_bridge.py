"""External shared-memory bridge contract helpers.

The bridge exports reviewed durable facts from SQLite truth into a neutral payload
that another backend can import. It is read-only and local-first: Scope Recall's
SQLite rows remain authoritative, and temporary `general` scratch is never
exported by default.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Sequence

from .memory_quality import sensitivity_metadata
from .sql_store import record_governance_audit_event

EXPORT_SCHEMA_VERSION = "external_shared_memory_export.v1"
DURABLE_EXPORT_TARGETS = ("user", "memory", "project", "ops")
HIDDEN_EXPORT_LIFECYCLES = {"archived", "candidate", "in_progress", "obsolete", "rejected", "superseded"}
CONFLICT_POLICIES = {"manual_review", "prefer_local", "prefer_newer"}


def _json_loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _target_list(values: Iterable[str] | None) -> list[str]:
    allowed = set(DURABLE_EXPORT_TARGETS)
    if values is None:
        return list(DURABLE_EXPORT_TARGETS)
    output: list[str] = []
    for value in values:
        target = str(value or "").strip().lower()
        if target in allowed and target not in output:
            output.append(target)
    return output or list(DURABLE_EXPORT_TARGETS)


def _scope_clause(accessible_scope_ids: Sequence[str] | None) -> tuple[str, list[str]]:
    if accessible_scope_ids is None:
        return "", []
    scopes = [str(item) for item in accessible_scope_ids if str(item)]
    if not scopes:
        return " AND 0", []
    return f" AND scope_id IN ({','.join('?' for _ in scopes)})", scopes


def validate_conflict_policy(conflict_policy: str) -> str:
    policy = str(conflict_policy or "").strip().lower()
    if policy not in CONFLICT_POLICIES:
        raise ValueError(f"conflict_policy is required and must be one of: {', '.join(sorted(CONFLICT_POLICIES))}")
    return policy


def build_external_memory_export(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str] | None,
    conflict_policy: str,
    targets: Iterable[str] | None = None,
    limit: int = 500,
    record_audit: bool = False,
    actor: str = "scope-recall:external-bridge",
    batch_id: str = "",
) -> dict[str, Any]:
    """Build a read-only export payload for durable shared-memory backends."""
    policy = validate_conflict_policy(conflict_policy)
    export_targets = _target_list(targets)
    target_placeholders = ",".join("?" for _ in export_targets)
    hidden_placeholders = ",".join("?" for _ in HIDDEN_EXPORT_LIFECYCLES)
    scope_sql, scope_params = _scope_clause(accessible_scope_ids)
    rows = conn.execute(
        f"""
        SELECT id, scope_id, source, target, content, summary, created_at, updated_at, metadata
        FROM memories
        WHERE target IN ({target_placeholders}){scope_sql}
          AND LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') END, 'promoted'))
              NOT IN ({hidden_placeholders})
        ORDER BY updated_at DESC, id ASC
        LIMIT ?
        """,
        [*export_targets, *scope_params, *sorted(HIDDEN_EXPORT_LIFECYCLES), max(1, int(limit))],
    ).fetchall()
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        metadata = _json_loads(row["metadata"])
        lifecycle = str(metadata.get("lifecycle") or "promoted").strip().lower()
        if lifecycle in HIDDEN_EXPORT_LIFECYCLES:
            skipped.append({"id": str(row["id"]), "reason": f"hidden_lifecycle:{lifecycle}"})
            continue
        sensitivity_info = sensitivity_metadata(metadata, content=str(row["content"] or ""))
        sensitivity = str(sensitivity_info.get("sensitivity") or "internal")
        sensitivity_reason = str(sensitivity_info.get("sensitivity_reason") or "")
        if sensitivity_reason == "plaintext_secret_rejected":
            skipped.append({"id": str(row["id"]), "reason": "plaintext_secret_rejected"})
            continue
        if sensitivity in {"restricted", "secret_reference"}:
            skipped.append({"id": str(row["id"]), "reason": f"sensitivity:{sensitivity}"})
            continue
        source_trust = metadata.get("source_trust", metadata.get("trust", 0.5))
        try:
            source_trust_value = max(0.0, min(1.0, float(source_trust)))
        except (TypeError, ValueError):
            source_trust_value = 0.5
        records.append(
            {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "id": str(row["id"]),
                "target": str(row["target"]),
                "content": str(row["content"]),
                "summary": str(row["summary"]),
                "metadata": {
                    "memory_type": str(metadata.get("memory_type") or metadata.get("category") or ""),
                    "importance": metadata.get("importance"),
                    "trust": metadata.get("trust"),
                    "confidence": metadata.get("confidence"),
                    "entities": metadata.get("entities") if isinstance(metadata.get("entities"), list) else [],
                    "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
                    "lifecycle": lifecycle or "promoted",
                    "sensitivity": sensitivity,
                },
                "provenance": {
                    "scope_id": str(row["scope_id"]),
                    "source": str(row["source"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "source_trust": source_trust_value,
                    "origin": "scope-recall-sqlite",
                },
                "conflict_policy": policy,
            }
        )
    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "ok": True,
        "read_only": not record_audit,
        "conflict_policy": policy,
        "allowed_targets": list(DURABLE_EXPORT_TARGETS),
        "count": len(records),
        "records": records,
        "skipped": {"count": len(skipped), "items": skipped},
    }
    if record_audit:
        audit_event_id = f"external-export-{uuid.uuid4().hex}"
        record_governance_audit_event(
            conn,
            event_id=audit_event_id,
            event_type="external_memory_export",
            action="export",
            batch_id=batch_id,
            before={},
            after={
                "schema_version": EXPORT_SCHEMA_VERSION,
                "conflict_policy": policy,
                "record_ids": [record["id"] for record in records],
                "count": len(records),
                "skipped_count": len(skipped),
            },
            reason="external shared-memory export",
            actor=actor,
            dry_run=False,
        )
        conn.commit()
        payload["audit_event_id"] = audit_event_id
    return payload
