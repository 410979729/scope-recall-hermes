#!/usr/bin/env python3
"""Review and optionally promote ordinary Scope Recall candidate memories.

This script closes the lifecycle gap between journal/digest extraction and the
profile surface. Default mode is read-only dry-run. Pass --apply to promote safe
candidate rows. Low-value archival decisions are never applied unless
--archive-noise is also provided.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any


def _ensure_source_import() -> None:
    here = Path(__file__).resolve()
    root = here.parents[1]
    parent = root.parent
    for path in (str(parent), str(root)):
        if path not in sys.path:
            sys.path.insert(0, path)


_ensure_source_import()

from scope_recall.candidate_promotion import candidate_debt_report, candidate_rows, classify_candidate_row, load_metadata, now_iso  # noqa: E402
from scope_recall.capture_filters import sanitize_report_text  # noqa: E402
from scope_recall.maintenance_ops import connect_memory_db, effective_apply, memory_db_path  # noqa: E402
from scope_recall.sql_store import ensure_governance_schema, record_governance_audit_event  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote safe scope-recall candidate memories")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--apply", action="store_true", help="apply safe promotions; default is read-only dry-run")
    parser.add_argument("--dry-run", action="store_true", help="explicit read-only dry-run (default; accepted for operator convenience)")
    parser.add_argument("--archive-noise", action="store_true", help="with --apply, archive rows classified as low-value noise")
    parser.add_argument(
        "--action",
        choices=["all", "promote_safe", "archive_low_value", "needs_review_high_risk", "defer_recent", "needs_review", "promote", "archive", "keep_candidate"],
        default="all",
        help="review/apply only one candidate lane or legacy action",
    )
    parser.add_argument("--limit", type=int, default=1000, help="maximum candidate rows to review")
    parser.add_argument("--batch-id", default="", help="optional governance batch id")
    parser.add_argument("--review-ids-file", default="", help="JSON/JSONL/text file containing explicit candidate ids reviewed by an operator")
    parser.add_argument("--review-decision", choices=["", "promote", "archive"], default="", help="operator decision for ids from --review-ids-file")
    parser.add_argument("--review-reason", default="", help="required human-readable reason for --review-ids-file decisions")
    parser.add_argument("--json", action="store_true", help="emit JSON output (accepted for operator convenience)")
    return parser.parse_args()


def _db_path(hermes_home: Path) -> Path:
    return memory_db_path(hermes_home)


def _metadata_after(metadata: dict[str, Any], *, action: str, reason: str, batch_id: str, at: str) -> dict[str, Any]:
    updated = dict(metadata)
    if action == "promote":
        updated["lifecycle"] = "promoted"
        updated["promoted_at"] = at
        updated["promoted_by"] = "candidate-promotion"
        updated["promotion_reason"] = reason
        updated["candidate_promotion_batch_id"] = batch_id
    elif action == "archive":
        updated["lifecycle"] = "archived"
        updated["archived_at"] = at
        updated["archived_by"] = "candidate-promotion"
        updated["archive_reason"] = reason
        updated["candidate_promotion_batch_id"] = batch_id
    return updated


def _audit_payload(row: sqlite3.Row, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "summary": str(row["summary"] or "")[:500],
        "updated_at": str(row["updated_at"] or ""),
        "metadata": metadata,
    }


def _load_review_ids_file(path: str) -> list[str]:
    if not path:
        return []
    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        return []
    ids: list[str] = []
    if raw.startswith("["):
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError("--review-ids-file JSON payload must be a list")
        for item in payload:
            value = item.get("id") if isinstance(item, dict) else item
            text = str(value or "").strip()
            if text:
                ids.append(text)
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("{"):
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("JSONL review rows must be objects with an id field")
                line = str(payload.get("id") or "").strip()
            else:
                line = line.split()[0].strip()
            if line:
                ids.append(line)
    return list(dict.fromkeys(ids))


def promote_memory_candidates(
    hermes_home: Path,
    *,
    apply: bool = False,
    archive_noise: bool = False,
    action: str = "all",
    limit: int = 1000,
    batch_id: str = "",
    review_ids: list[str] | None = None,
    review_decision: str = "",
    review_reason: str = "",
) -> dict[str, Any]:
    """Plan and optionally apply candidate-memory promotion from the operator CLI.

    The function separates classification, review output, archive-noise decisions, and writes so a dry run gives the same plan an apply run would execute."""
    db_path = _db_path(hermes_home)
    if not db_path.exists():
        return {"ok": False, "status": "missing", "path": str(db_path), "error": "SQLite truth DB not found"}

    batch = batch_id or f"candidate-promotion-{now_iso().replace(':', '').replace('+', 'Z')}"
    explicit_review_ids = list(dict.fromkeys(str(item).strip() for item in (review_ids or []) if str(item).strip()))
    explicit_review_decision = str(review_decision or "").strip().lower()
    explicit_review_reason = str(review_reason or "").strip()
    if explicit_review_ids:
        if explicit_review_decision not in {"promote", "archive"}:
            return {"ok": False, "status": "invalid", "error": "--review-decision must be promote or archive when --review-ids-file is provided"}
        if not explicit_review_reason:
            return {"ok": False, "status": "invalid", "error": "--review-reason is required when --review-ids-file is provided"}
    conn = connect_memory_db(db_path, apply=apply, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    try:
        if apply:
            ensure_governance_schema(conn)
        before = candidate_debt_report(conn, limit=limit)
        rows = candidate_rows(conn, limit=limit)
        reviewed: list[dict[str, Any]] = []
        mutations = {"promoted": 0, "archived": 0, "kept": 0, "skipped": 0}
        at = now_iso()
        action_filter = str(action or "all").strip().lower()
        row_ids = {str(row["id"]) for row in rows}
        missing_review_ids = [item for item in explicit_review_ids if item not in row_ids]
        if missing_review_ids:
            return {"ok": False, "status": "invalid", "error": "review ids are not current candidates", "missing_review_ids": missing_review_ids}
        review_id_set = set(explicit_review_ids)
        for row in rows:
            decision = classify_candidate_row(row)
            row_id = str(row["id"])
            if explicit_review_ids:
                if row_id not in review_id_set:
                    continue
            elif action_filter != "all" and action_filter not in {decision.lane, decision.action}:
                continue
            effective_action = decision.action
            effective_reason = decision.reason
            explicit_archive_lane = action_filter in {"archive_low_value", "archive"}
            if explicit_review_ids:
                effective_action = explicit_review_decision
                effective_reason = f"operator_review:{explicit_review_reason}"
            elif decision.action == "archive" and not (archive_noise or explicit_archive_lane):
                effective_action = "keep_candidate"
            item = {
                "id": row_id,
                "target": str(row["target"] or ""),
                "source": str(row["source"] or ""),
                "decision": decision.action,
                "lane": decision.lane,
                "effective_action": effective_action,
                "reason": effective_reason,
                "classifier_reason": decision.reason,
                "risk": decision.risk,
                "confidence": decision.confidence,
                "importance": decision.importance,
                "memory_type": decision.memory_type,
                "updated_at": str(row["updated_at"] or ""),
                "summary": sanitize_report_text(str(row["summary"] or ""))[:220],
            }
            reviewed.append(item)
            if not apply:
                if effective_action == "promote":
                    mutations["promoted"] += 1
                elif effective_action == "archive":
                    mutations["archived"] += 1
                elif effective_action == "skip":
                    mutations["skipped"] += 1
                else:
                    mutations["kept"] += 1
                continue
            if effective_action not in {"promote", "archive"}:
                if effective_action == "skip":
                    mutations["skipped"] += 1
                else:
                    mutations["kept"] += 1
                continue

            before_metadata = load_metadata(row["metadata"])
            after_metadata = _metadata_after(before_metadata, action=effective_action, reason=effective_reason, batch_id=batch, at=at)
            cursor = conn.execute(
                """
                UPDATE memories
                SET metadata = ?, updated_at = ?
                WHERE id = ?
                  AND LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') ELSE '' END, '')) = 'candidate'
                """,
                (json.dumps(after_metadata, ensure_ascii=False, sort_keys=True), at, str(row["id"])),
            )
            if cursor.rowcount != 1:
                item["effective_action"] = "skip"
                item["skip_reason"] = "row_not_updated"
                mutations["skipped"] += 1
                continue
            if effective_action == "promote":
                mutations["promoted"] += 1
            else:
                mutations["archived"] += 1
            record_governance_audit_event(
                conn,
                event_id=f"govevt_{uuid.uuid4().hex}",
                event_type="memory_candidate_promotion",
                action=effective_action,
                scope_id=str(row["scope_id"] or ""),
                target_id=str(row["id"]),
                batch_id=batch,
                before=_audit_payload(row, before_metadata),
                after=_audit_payload(row, after_metadata),
                reason=effective_reason,
                actor="scripts/promote.memory_candidates.py",
                dry_run=False,
                created_at=at,
            )
        if apply:
            conn.commit()
        after = candidate_debt_report(conn, limit=limit)
    finally:
        conn.close()

    return {
        "ok": True,
        "status": "applied" if apply else "dry_run",
        "dry_run": not apply,
        "path": str(db_path),
        "batch_id": batch,
        "archive_noise": bool(archive_noise),
        "action_filter": str(action or "all").strip().lower(),
        "operator_review": {
            "ids": explicit_review_ids,
            "decision": explicit_review_decision,
            "reason": explicit_review_reason,
        },
        "before": before,
        "mutations": mutations,
        "after": after,
        "reviewed": reviewed,
    }


def main() -> int:
    args = parse_args()
    try:
        review_ids = _load_review_ids_file(str(args.review_ids_file or ""))
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "invalid", "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    payload = promote_memory_candidates(
        Path(args.hermes_home),
        apply=effective_apply(apply=args.apply, dry_run=args.dry_run),
        archive_noise=bool(args.archive_noise),
        action=str(args.action or "all"),
        limit=max(1, int(args.limit or 1000)),
        batch_id=str(args.batch_id or ""),
        review_ids=review_ids,
        review_decision=str(args.review_decision or ""),
        review_reason=str(args.review_reason or ""),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
