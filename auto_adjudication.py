"""Scheduled no-human-in-the-loop candidate adjudication.

Memory auditing must not depend on a human reviewing items one by one.
This module turns the existing candidate classification lanes into a
scheduled pipeline:

- L1-L3 (deterministic lanes): ``promote_safe`` rows old enough to trust are
  promoted; ``archive_low_value`` rows are archived. Both reuse the same
  classifier, lifecycle CAS transition, and governance batch receipts as the
  operator CLI, so an auto decision is indistinguishable from a reviewed one
  in the audit trail.
- L4 (grounded sampling review): a bounded budget of held/needs-review
  candidates is re-examined against complete journal evidence by an LLM. The
  result is advisory only: untrusted model output never owns memory lifecycle.
- L5 (exception surface): every run returns one bounded summary dict that
  doctor/stats expose; humans read summaries, never queues.

The adjudicator never runs inside the provider hot path. It must hold the
cross-process truth writer lease before opening a writable pager, and it
must release leftover snapshot transactions before any L4 LLM call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .adjudication_l4 import (
    build_review_request,
    collect_journal_evidence,
    parse_l4_response,
)
from .adjudication_progress import (
    L4ReviewReceipt,
    advisory_queue_id,
    candidate_review_fingerprint,
    latest_queue_cursor,
    latest_scan_cursor,
    record_review_receipts,
    record_scan_cursor,
    reviewed_fingerprints,
)
from .candidate_promotion import (
    candidate_rows,
    classify_candidate_row,
    load_metadata,
    now_iso,
)
from .candidate_review import transition_candidate_metadata
from .adjudication_schedule import (
    claim_adjudication_schedule,
    complete_adjudication_schedule,
    latest_schedule_retry_context,
    release_adjudication_schedule,
    retry_adjudication_schedule,
    schedule_target_id,
)
from .capture_filters import sanitize_report_text
from .lifecycle_service import LifecycleConflictError, transition_memory_lifecycle
from .maintenance_ops import connect_memory_db, memory_db_path
from .sql_store import ensure_governance_schema
from .transaction_guard import prepare_network_boundary
from .writer_lease import TruthWriterBusyError, holding_truth_writer_lease

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "interval_hours": 24,
    "claim_timeout_hours": 2,
    "retry_backoff_minutes": 15,
    "promote_min_age_hours": 24,
    "max_promotions_per_run": 100,
    "max_archives_per_run": 200,
    "l4_enabled": True,
    "l4_budget_per_run": 20,
    "l4_max_evidence_chars": 2400,
}


class L4ConfigurationError(RuntimeError):
    """The requested L4 reviewer could not be built from runtime config."""


def _age_hours(updated_at: str) -> float:
    try:
        stamp = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0)


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return max(0, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


_journal_evidence = collect_journal_evidence


def _transition(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    action: str,
    reason: str,
    batch_id: str,
    at: str,
) -> bool:
    metadata_before = load_metadata(row["metadata"])
    metadata_after = transition_candidate_metadata(
        metadata_before,
        action=action,
        actor="auto-adjudication",
        reason=reason,
        timestamp=at,
        batch_id=batch_id,
    )
    try:
        transition_memory_lifecycle(
            conn,
            memory_id=str(row["id"]),
            lifecycle=str(metadata_after["lifecycle"]),
            metadata_updates=metadata_after,
            expected_updated_at=str(row["updated_at"] or ""),
            expected_lifecycle=str(metadata_before.get("lifecycle") or "candidate"),
            actor="auto_adjudication",
            reason=reason,
            event_type="memory_auto_adjudication",
            action=action,
            batch_id=batch_id,
            timestamp=at,
        )
        return True
    except LifecycleConflictError:
        return False


def _checkpoint_l4_progress(
    *,
    hermes_home: Path,
    db_path: Path,
    receipts: Sequence[L4ReviewReceipt],
    batch_id: str,
    at: str,
    queue_id: str,
    last_selected_id: str,
) -> None:
    """Persist a bounded L4 receipt/cursor checkpoint in one short transaction."""

    with holding_truth_writer_lease(
        Path(hermes_home) / "scope-recall", role="auto_adjudication_progress"
    ):
        write_conn = connect_memory_db(db_path, apply=True, timeout=30.0)
        write_conn.row_factory = sqlite3.Row
        try:
            ensure_governance_schema(write_conn)
            write_conn.commit()
            write_conn.execute("BEGIN IMMEDIATE")
            record_review_receipts(
                write_conn,
                receipts,
                batch_id=batch_id,
                created_at=at,
                queue_id=queue_id,
                last_selected_id=last_selected_id,
            )
            write_conn.commit()
        except Exception:
            if write_conn.in_transaction:
                write_conn.rollback()
            raise
        finally:
            write_conn.close()


def _run_l4_advisory(
    *,
    hermes_home: Path,
    db_path: Path,
    rows: list[dict[str, Any]],
    llm_call: Callable[..., str],
    budget: int,
    evidence_chars: int,
    summary: dict[str, Any],
    batch_id: str,
    at: str,
    queue_id: str,
    scope_ids: Sequence[str],
    all_scopes: bool,
) -> None:
    """Review held candidates without owning truth-writer or lifecycle authority."""

    conn = connect_memory_db(db_path, apply=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    last_selected_id = ""
    last_checkpointed_id = ""
    checkpoint_failed = False

    def retain_retry_rows(pending_rows: Sequence[dict[str, Any]]) -> None:
        """Keep uncheckpointed advisory work in stable retry order."""

        retry_ids = summary["_l4_retry_candidate_ids"]
        seen = {str(value) for value in retry_ids}
        for pending_row in pending_rows:
            memory_id = str(pending_row.get("id") or "")
            if memory_id and memory_id not in seen:
                retry_ids.append(memory_id)
                seen.add(memory_id)

    try:
        rows.sort(
            key=lambda row: (
                str(row.get("updated_at") or ""),
                str(row.get("id") or ""),
            )
        )
        cursor = latest_queue_cursor(conn, queue_id)
        ordered_ids = [str(row.get("id") or "") for row in rows]
        if cursor in ordered_ids:
            split = ordered_ids.index(cursor) + 1
            rows = [*rows[split:], *rows[:split]]
        prior = reviewed_fingerprints(
            conn, [str(row.get("id") or "") for row in rows]
        )
        for row_index, row in enumerate(rows):
            if summary["l4"]["selected"] >= budget:
                retain_retry_rows(rows[row_index:])
                break
            memory_id = str(row.get("id") or "")
            try:
                evidence = _journal_evidence(
                    conn,
                    memory_id,
                    scope_ids=scope_ids,
                    all_scopes=all_scopes,
                    max_chars=evidence_chars,
                )
            except sqlite3.OperationalError as exc:
                summary["l4"]["selected"] += 1
                last_selected_id = memory_id
                summary["l4"]["errors"] += 1
                summary["exceptions"].append(
                    {
                        "kind": "l4_evidence_lookup",
                        "id": memory_id,
                        "error": sanitize_report_text(str(exc))[:160],
                    }
                )
                summary["_l4_retry_candidate_ids"].append(memory_id)
                continue
            fingerprint = candidate_review_fingerprint(row, evidence)
            if (memory_id, fingerprint) in prior:
                continue
            summary["l4"]["selected"] += 1
            last_selected_id = memory_id
            if evidence.authorization_error:
                summary["l4"]["errors"] += 1
                summary["l4"]["evidence_incomplete"] += 1
                summary["l4"]["scope_violations"] += 1
                summary["exceptions"].append(
                    {
                        "kind": "l4_evidence_scope_violation",
                        "id": memory_id,
                        "error": "linked journal evidence is outside authorized scopes",
                    }
                )
                continue
            if evidence.truncated:
                summary["l4"]["evidence_truncated"] += 1
                summary["l4"]["evidence_incomplete"] += 1
                summary["l4"]["destructive_blocked_truncated"] += 1
                continue
            if evidence.total_count == 0 or evidence.included_count == 0:
                summary["l4"]["errors"] += 1
                summary["l4"]["evidence_incomplete"] += 1
                summary["exceptions"].append(
                    {
                        "kind": "l4_evidence_incomplete",
                        "id": memory_id,
                        "error": "no linked journal evidence",
                    }
                )
                continue
            request = build_review_request(
                target=str(row.get("target") or ""),
                memory_type=str(
                    load_metadata(row.get("metadata")).get("memory_type") or ""
                ),
                content=str(row.get("content") or "")[:1200],
                evidence_text=evidence.text,
                evidence_truncated=evidence.truncated,
            )
            try:
                prepare_network_boundary(conn, "auto_adjudication.l4_llm")
                summary["l4"]["attempted"] += 1
                raw_verdict = llm_call(
                    request.user_payload,
                    system_prompt=request.system_prompt,
                )
            except Exception as exc:
                summary["l4"]["errors"] += 1
                summary["exceptions"].append(
                    {
                        "kind": "l4_llm_error",
                        "id": memory_id,
                        "error": sanitize_report_text(str(exc))[:160],
                    }
                )
                summary["_l4_retry_candidate_ids"].append(memory_id)
                continue
            parsed = parse_l4_response(raw_verdict)
            if not parsed.ok or parsed.verdict is None:
                summary["l4"]["errors"] += 1
                summary["l4"]["protocol_errors"] += 1
                summary["exceptions"].append(
                    {
                        "kind": "l4_protocol_error",
                        "id": memory_id,
                        "error": parsed.error,
                    }
                )
                summary["_l4_retry_candidate_ids"].append(memory_id)
                continue
            fresh_row_raw = conn.execute(
                "SELECT id, scope_id, target, content, metadata, updated_at "
                "FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            fresh_row = dict(fresh_row_raw) if fresh_row_raw is not None else None
            if fresh_row is None or any(
                str(fresh_row.get(key) or "") != str(row.get(key) or "")
                for key in ("scope_id", "content", "metadata", "updated_at")
            ):
                summary["l4"]["conflicts_skipped"] += 1
                continue
            fresh_evidence = _journal_evidence(
                conn,
                memory_id,
                scope_ids=scope_ids,
                all_scopes=all_scopes,
                max_chars=evidence_chars,
            )
            if fresh_evidence != evidence:
                summary["l4"]["conflicts_skipped"] += 1
                continue
            verdict = parsed.verdict
            summary["l4"]["reviewed"] += 1
            summary["l4"][verdict] += 1
            summary["l4"]["advisory_only"] += 1
            receipt = L4ReviewReceipt(
                memory_id=memory_id,
                scope_id=str(row.get("scope_id") or ""),
                review_fingerprint=fingerprint,
                verdict=verdict,
                reason=parsed.reason,
            )
            try:
                _checkpoint_l4_progress(
                    hermes_home=hermes_home,
                    db_path=db_path,
                    receipts=(receipt,),
                    batch_id=batch_id,
                    at=at,
                    queue_id=queue_id,
                    last_selected_id=memory_id,
                )
            except Exception as exc:
                # A verdict is not durable until its checkpoint commits. Roll back
                # the in-memory success counters and retain this row plus the tail.
                summary["l4"]["reviewed"] -= 1
                summary["l4"][verdict] -= 1
                summary["l4"]["advisory_only"] -= 1
                summary["l4"]["errors"] += 1
                summary["exceptions"].append(
                    {
                        "kind": "l4_checkpoint_error",
                        "id": memory_id,
                        "error": sanitize_report_text(str(exc))[:160],
                    }
                )
                retain_retry_rows(rows[row_index:])
                checkpoint_failed = True
                break
            last_checkpointed_id = memory_id
    finally:
        conn.close()

    if (
        not checkpoint_failed
        and last_selected_id
        and last_selected_id != last_checkpointed_id
    ):
        try:
            _checkpoint_l4_progress(
                hermes_home=hermes_home,
                db_path=db_path,
                receipts=(),
                batch_id=batch_id,
                at=at,
                queue_id=queue_id,
                last_selected_id=last_selected_id,
            )
        except Exception as exc:
            # Cursor advancement is part of the durable L4 checkpoint. If it
            # fails, preserve every selected row not already represented by a
            # committed receipt so the scheduler can retry it.
            summary["l4"]["errors"] += 1
            summary["exceptions"].append(
                {
                    "kind": "l4_checkpoint_error",
                    "id": last_selected_id,
                    "error": sanitize_report_text(str(exc))[:160],
                }
            )
            row_ids = [str(row.get("id") or "") for row in rows]
            retry_from = (
                row_ids.index(last_checkpointed_id) + 1
                if last_checkpointed_id in row_ids
                else 0
            )
            retain_retry_rows(rows[retry_from:])


def run_auto_adjudication(
    hermes_home: Path,
    runtime_config: dict[str, Any] | None = None,
    *,
    llm_call: Callable[..., str] | None = None,
    limit: int = 1000,
    scope_ids: Sequence[str] | None = None,
    all_scopes: bool = False,
) -> dict[str, Any]:
    """Run one bounded adjudication pass inside an explicit write boundary.

    Provider-triggered runs pass their immutable writable-scope allowlist.
    ``all_scopes`` is reserved for an explicit operator invocation; omitting
    both modes fails closed before the truth database is opened.
    """

    raw = (runtime_config or {}).get("auto_adjudication")
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        config.update(raw)
    if not bool(config.get("enabled", True)):
        return {
            "ok": True,
            "status": "disabled",
            "lanes": {},
            "l4": {
                "enabled": False,
                "errors": 0,
                "exhausted_archived": 0,
                "conflicts_skipped": 0,
            },
        }

    normalized_scope_ids = tuple(
        dict.fromkeys(
            str(scope_id).strip()
            for scope_id in (scope_ids or ())
            if str(scope_id).strip()
        )
    )
    if all_scopes and normalized_scope_ids:
        return {"ok": False, "status": "ambiguous_scope_mode"}
    if not all_scopes and not normalized_scope_ids:
        return {"ok": False, "status": "scope_required"}
    queue_id = advisory_queue_id(normalized_scope_ids, all_scopes=all_scopes)

    db_path = memory_db_path(Path(hermes_home))
    if not db_path.exists():
        return {"ok": False, "status": "missing_database", "path": str(db_path)}

    batch_id = f"auto-adjudication-{uuid.uuid4().hex[:12]}"
    at = now_iso()
    promote_cap = _config_int(config, "max_promotions_per_run", 100)
    archive_cap = _config_int(config, "max_archives_per_run", 200)
    min_age_hours = float(config.get("promote_min_age_hours") or 24)
    l4_budget = _config_int(config, "l4_budget_per_run", 20)
    l4_evidence_chars = _config_int(config, "l4_max_evidence_chars", 2400)
    l4_enabled = bool(config.get("l4_enabled", True)) and llm_call is not None

    summary: dict[str, Any] = {
        "ok": True,
        "status": "applied",
        "lanes_status": "pending",
        "batch_id": batch_id,
        "at": at,
        "lanes": {
            "promoted": 0,
            "promoted_attempted": 0,
            "promote_deferred_young": 0,
            "archived": 0,
            "archived_attempted": 0,
            "rolled_back": 0,
            "held_for_l4": 0,
            "defer_recent": 0,
            "skipped": 0,
            "conflicts_skipped": 0,
        },
        "l4": {
            "enabled": l4_enabled,
            "status": "pending" if l4_enabled else "disabled",
            "reviewed": 0,
            "attempted": 0,
            "selected": 0,
            "supported": 0,
            "unsupported": 0,
            "uncertain": 0,
            "exhausted_archived": 0,
            "advisory_only": 0,
            "conflicts_skipped": 0,
            "errors": 0,
            "protocol_errors": 0,
            "evidence_incomplete": 0,
            "evidence_truncated": 0,
            "destructive_blocked_truncated": 0,
            "scope_violations": 0,
        },
        "_l4_candidate_ids": [],
        "_l4_retry_candidate_ids": [],
        "exceptions": [],
    }
    l4_pool: list[dict[str, Any]] = []

    try:
        with holding_truth_writer_lease(
            Path(hermes_home) / "scope-recall", role="auto_adjudication"
        ):
            conn = connect_memory_db(db_path, apply=True, timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.row_factory = sqlite3.Row
            try:
                ensure_governance_schema(conn)
                scan_updated_at, scan_id = latest_scan_cursor(conn, queue_id)
                rows = candidate_rows(
                    conn,
                    scope_ids=None if all_scopes else normalized_scope_ids,
                    limit=limit,
                    cursor_updated_at=scan_updated_at,
                    cursor_id=scan_id,
                )
                for row in rows:
                    decision = classify_candidate_row(row, conn)
                    if decision.lane == "promote_safe":
                        if _age_hours(str(row["updated_at"] or "")) < min_age_hours:
                            summary["lanes"]["promote_deferred_young"] += 1
                            continue
                        if summary["lanes"]["promoted"] >= promote_cap:
                            continue
                        if _transition(
                            conn,
                            row,
                            action="promote",
                            reason=f"auto:{decision.reason}",
                            batch_id=batch_id,
                            at=at,
                        ):
                            summary["lanes"]["promoted"] += 1
                            summary["lanes"]["promoted_attempted"] += 1
                        else:
                            summary["lanes"]["conflicts_skipped"] += 1
                    elif decision.lane == "archive_low_value":
                        if summary["lanes"]["archived"] >= archive_cap:
                            continue
                        if _transition(
                            conn,
                            row,
                            action="archive",
                            reason=f"auto:{decision.reason}",
                            batch_id=batch_id,
                            at=at,
                        ):
                            summary["lanes"]["archived"] += 1
                            summary["lanes"]["archived_attempted"] += 1
                        else:
                            summary["lanes"]["conflicts_skipped"] += 1
                    elif decision.lane == "defer_recent":
                        summary["lanes"]["defer_recent"] += 1
                    elif decision.lane == "skip":
                        summary["lanes"]["skipped"] += 1
                    else:
                        summary["lanes"]["held_for_l4"] += 1
                        l4_pool.append(dict(row))

                summary["_l4_candidate_ids"] = [
                    str(row.get("id") or "") for row in l4_pool
                ]

                if rows:
                    last_row = rows[-1]
                    record_scan_cursor(
                        conn,
                        queue_id=queue_id,
                        updated_at=str(last_row["updated_at"] or ""),
                        memory_id=str(last_row["id"] or ""),
                        batch_id=batch_id,
                        created_at=at,
                    )
                conn.commit()
                summary["lanes_status"] = "committed"
            except Exception as exc:
                conn.rollback()
                summary["lanes"]["rolled_back"] = (
                    summary["lanes"]["promoted"]
                    + summary["lanes"]["archived"]
                )
                summary["lanes"]["promoted"] = 0
                summary["lanes"]["archived"] = 0
                summary["ok"] = False
                summary["status"] = "failed"
                summary["lanes_status"] = "rolled_back"
                summary["error"] = sanitize_report_text(str(exc))[:300]
                logger.exception("Scope Recall auto adjudication failed")
            finally:
                conn.close()
        if l4_enabled and l4_pool:
            assert llm_call is not None
            _run_l4_advisory(
                hermes_home=Path(hermes_home),
                db_path=db_path,
                rows=l4_pool,
                llm_call=llm_call,
                budget=l4_budget,
                evidence_chars=l4_evidence_chars,
                summary=summary,
                batch_id=batch_id,
                at=at,
                queue_id=queue_id,
                scope_ids=normalized_scope_ids,
                all_scopes=all_scopes,
            )
            if summary["l4"]["selected"]:
                if summary["l4"]["errors"] and not summary["l4"]["reviewed"]:
                    summary["l4"]["status"] = "failed"
                    if summary["lanes_status"] == "committed":
                        summary["status"] = "applied_l4_degraded"
                elif summary["l4"]["errors"]:
                    summary["l4"]["status"] = "partial"
                    if summary["lanes_status"] == "committed":
                        summary["status"] = "applied_l4_degraded"
                else:
                    summary["l4"]["status"] = "ok"
            else:
                summary["l4"]["status"] = "idle"
    except TruthWriterBusyError:
        summary["ok"] = False
        summary["status"] = "truth_writer_busy"
    except Exception as exc:
        summary["ok"] = False
        summary["status"] = "failed"
        summary["error"] = sanitize_report_text(str(exc))[:300]
        logger.exception("Scope Recall auto adjudication failed")
    return summary


def _load_l4_retry_rows(
    conn: sqlite3.Connection,
    *,
    candidate_ids: Sequence[str],
    scope_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Reload exact queued candidates without re-running deterministic lanes."""

    ordered_ids = tuple(
        dict.fromkeys(str(value).strip() for value in candidate_ids if str(value).strip())
    )
    scopes = tuple(
        dict.fromkeys(str(value).strip() for value in scope_ids if str(value).strip())
    )
    if not ordered_ids or not scopes:
        return []
    found: dict[str, dict[str, Any]] = {}
    scope_placeholders = ", ".join("?" for _ in scopes)
    for offset in range(0, len(ordered_ids), 300):
        batch = ordered_ids[offset : offset + 300]
        id_placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT id, scope_id, source, target, content, summary, updated_at, metadata
            FROM memories
            WHERE id IN ({id_placeholders})
              AND scope_id IN ({scope_placeholders})
              AND LOWER(COALESCE(
                    CASE WHEN json_valid(metadata)
                         THEN json_extract(metadata, '$.lifecycle') ELSE '' END,
                    ''
                  )) = 'candidate'
            """,
            (*batch, *scopes),
        ).fetchall()
        for row in rows:
            item = dict(row)
            found[str(item.get("id") or "")] = item
    return [found[memory_id] for memory_id in ordered_ids if memory_id in found]


def run_l4_retry(
    hermes_home: Path,
    runtime_config: dict[str, Any] | None,
    *,
    llm_call: Callable[..., str],
    candidate_ids: Sequence[str],
    scope_ids: Sequence[str],
) -> dict[str, Any]:
    """Retry only queued advisory L4 work; deterministic lanes are untouched."""

    normalized_scope_ids = tuple(
        dict.fromkeys(
            str(scope_id).strip() for scope_id in scope_ids if str(scope_id).strip()
        )
    )
    if not normalized_scope_ids:
        return {"ok": False, "status": "scope_required", "lanes_status": "not_run"}
    db_path = memory_db_path(Path(hermes_home))
    if not db_path.exists():
        return {"ok": False, "status": "missing_database", "lanes_status": "not_run"}
    raw = (runtime_config or {}).get("auto_adjudication")
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        config.update(raw)
    batch_id = f"auto-adjudication-l4-{uuid.uuid4().hex[:12]}"
    at = now_iso()
    summary: dict[str, Any] = {
        "ok": True,
        "status": "l4_applied",
        "lanes_status": "not_run",
        "batch_id": batch_id,
        "at": at,
        "lanes": {},
        "l4": {
            "enabled": True,
            "status": "pending",
            "reviewed": 0,
            "attempted": 0,
            "selected": 0,
            "supported": 0,
            "unsupported": 0,
            "uncertain": 0,
            "exhausted_archived": 0,
            "advisory_only": 0,
            "conflicts_skipped": 0,
            "errors": 0,
            "protocol_errors": 0,
            "evidence_incomplete": 0,
            "evidence_truncated": 0,
            "destructive_blocked_truncated": 0,
            "scope_violations": 0,
        },
        "_l4_retry_candidate_ids": [],
        "exceptions": [],
    }
    conn = connect_memory_db(db_path, apply=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = _load_l4_retry_rows(
            conn,
            candidate_ids=candidate_ids,
            scope_ids=normalized_scope_ids,
        )
    finally:
        conn.close()
    if not rows:
        summary["status"] = "l4_idle"
        summary["l4"]["status"] = "idle"
        return summary
    _run_l4_advisory(
        hermes_home=Path(hermes_home),
        db_path=db_path,
        rows=rows,
        llm_call=llm_call,
        budget=_config_int(config, "l4_budget_per_run", 20),
        evidence_chars=_config_int(config, "l4_max_evidence_chars", 2400),
        summary=summary,
        batch_id=batch_id,
        at=at,
        queue_id=advisory_queue_id(normalized_scope_ids, all_scopes=False),
        scope_ids=normalized_scope_ids,
        all_scopes=False,
    )
    if summary["l4"]["errors"] and not summary["l4"]["reviewed"]:
        summary["ok"] = False
        summary["status"] = "l4_failed"
        summary["l4"]["status"] = "failed"
    elif summary["l4"]["errors"]:
        summary["ok"] = False
        summary["status"] = "l4_partial"
        summary["l4"]["status"] = "partial"
    else:
        summary["l4"]["status"] = "ok"
    return summary


def build_l4_llm_call(
    hermes_home: Path, journal_config: dict[str, Any]
) -> Callable[..., str] | None:
    """Build the grounded-review LLM callable from the digest LLM settings.

    L4 reuses the journal digest provider/model (the same trusted extraction
    channel). A requested but invalid configuration is an operational failure,
    not permission to record a successful lanes-only schedule completion.
    """

    try:
        from datetime import date

        from .http_utils import explicit_insecure_endpoint_opt_in
        from .journal_llm import _call_llm_with_retries
        from .nightly_digest import DigestOptions, resolve_llm_config

        options = DigestOptions(
            hermes_home=Path(hermes_home),
            digest_date=date.today(),
            extractor="llm",
            chunk_chars=3000,
            max_session_chars=6000,
            provider=str(journal_config.get("provider") or journal_config.get("llm_provider") or ""),
            model=str(journal_config.get("model") or journal_config.get("llm_model") or ""),
            base_url=str(journal_config.get("base_url") or ""),
            endpoint=str(journal_config.get("endpoint") or ""),
            append_v1=bool(journal_config.get("append_v1", True)),
            allow_insecure_endpoint=(
                explicit_insecure_endpoint_opt_in(journal_config.get("allow_insecure_endpoint"))
                if "allow_insecure_endpoint" in journal_config
                else None
            ),
            api_key=str(journal_config.get("api_key") or ""),
            api_key_env=str(journal_config.get("api_key_env") or journal_config.get("key_env") or ""),
            api_mode=str(journal_config.get("api_mode") or ""),
            timeout=float(journal_config.get("timeout") or journal_config.get("llm_timeout") or 60.0),
        )
        llm_config = resolve_llm_config(Path(hermes_home), options)
    except Exception as exc:
        logger.exception(
            "Scope Recall L4 grounded review is unavailable: digest LLM config "
            "did not resolve"
        )
        raise L4ConfigurationError("digest LLM config did not resolve") from exc

    def call(prompt: str, *, system_prompt: str) -> str:
        return _call_llm_with_retries(
            prompt,
            model=llm_config["model"],
            base_url=llm_config["base_url"],
            api_key=llm_config["api_key"],
            timeout=options.timeout,
            api_mode=llm_config.get("api_mode", "chat_completions"),
            endpoint=str(llm_config.get("endpoint") or ""),
            append_v1=bool(llm_config.get("append_v1", True)),
            allow_insecure_endpoint=explicit_insecure_endpoint_opt_in(
                llm_config.get("allow_insecure_endpoint")
            ),
            thinking=(
                llm_config.get("thinking")
                if isinstance(llm_config.get("thinking"), dict)
                else None
            ),
            max_attempts=2,
            retry_delay=1.0,
            system_prompt=system_prompt,
        )

    return call


def run_provider_auto_adjudication(provider: Any, *, trigger: str) -> None:
    """Run deterministic lanes and advisory L4 on independent schedules."""

    import time

    from .gating import config_bool

    if provider._shutdown_requested.is_set() or provider._hermes_home is None:
        return
    if provider._truth_writes_blocked() or provider._memory_isolated_for_scope():
        return
    raw_config = provider._config.get("auto_adjudication")
    adjudication_config = raw_config if isinstance(raw_config, dict) else {}
    if not config_bool(adjudication_config, "enabled", True):
        return
    try:
        interval_hours = float(adjudication_config.get("interval_hours") or 24)
    except (TypeError, ValueError):
        interval_hours = 24.0
    try:
        claim_timeout_hours = float(
            adjudication_config.get("claim_timeout_hours") or 2
        )
    except (TypeError, ValueError):
        claim_timeout_hours = 2.0
    interval_hours = max(0.0, interval_hours)
    claim_timeout_hours = max(1.0 / 60.0, claim_timeout_hours)
    try:
        retry_backoff_minutes = float(
            adjudication_config.get("retry_backoff_minutes") or 15
        )
    except (TypeError, ValueError):
        retry_backoff_minutes = 15.0
    retry_backoff_seconds = min(
        24.0 * 3600.0,
        max(60.0, retry_backoff_minutes * 60.0),
    )
    now = time.time()
    db_path = memory_db_path(Path(provider._hermes_home))
    writable_scope_ids = tuple(provider._writable_scope_ids)
    target_id = schedule_target_id(writable_scope_ids)
    l4_enabled = config_bool(adjudication_config, "l4_enabled", True)
    l4_target_id = f"{target_id}:l4"
    l4_retry_context = (
        latest_schedule_retry_context(db_path, target_id=l4_target_id)
        if l4_enabled
        else {}
    )
    l4_retry_candidate_ids = tuple(
        str(value).strip()
        for value in (l4_retry_context.get("candidate_ids") or ())
        if str(value).strip()
    )
    claim_token = claim_adjudication_schedule(
        db_path,
        now=now,
        interval_hours=interval_hours,
        claim_timeout_hours=claim_timeout_hours,
        trigger=trigger,
        target_id=target_id,
    )
    l4_claim_token = None
    may_claim_l4 = l4_enabled and (
        claim_token is not None or bool(l4_retry_candidate_ids)
    )
    try:
        l4_claim_token = (
            claim_adjudication_schedule(
                db_path,
                now=now,
                interval_hours=interval_hours,
                claim_timeout_hours=claim_timeout_hours,
                trigger=f"{trigger}:l4",
                target_id=l4_target_id,
            )
            if may_claim_l4
            else None
        )
    except Exception:
        if claim_token is not None:
            try:
                release_adjudication_schedule(
                    db_path,
                    claim_token=claim_token,
                    released_at=time.time(),
                    trigger=trigger,
                    interval_hours=interval_hours,
                    target_id=target_id,
                )
            except Exception:
                logger.exception(
                    "Scope Recall failed to release primary adjudication claim "
                    "after L4 claim acquisition failed"
                )
        return
    if claim_token is not None and l4_enabled and l4_claim_token is None:
        try:
            release_adjudication_schedule(
                db_path,
                claim_token=claim_token,
                released_at=time.time(),
                trigger=trigger,
                interval_hours=interval_hours,
                target_id=target_id,
            )
        except Exception:
            logger.exception(
                "Scope Recall failed to release primary adjudication claim "
                "after the paired L4 claim was unavailable"
            )
        return
    if claim_token is None and l4_claim_token is None:
        return
    try:
        llm_call = None
        l4_config_error = False
        report: dict[str, Any] | None = None
        lanes_committed = False
        if l4_claim_token is not None:
            try:
                llm_call = build_l4_llm_call(
                    provider._hermes_home, provider._journal_config()
                )
            except L4ConfigurationError:
                l4_config_error = True
        if claim_token is not None:
            report = run_auto_adjudication(
                provider._hermes_home,
                provider._config,
                llm_call=llm_call if l4_claim_token is not None else None,
                scope_ids=writable_scope_ids,
            )
            lanes_committed = report.get("lanes_status") == "committed"
            if not report.get("lanes_status"):
                lanes_committed = bool(report.get("ok"))

        # L4 owns the retry candidate IDs, so publish its terminal action
        # before the primary schedule can be completed.
        if l4_claim_token is not None:
            if claim_token is not None:
                assert report is not None
                candidate_ids = tuple(
                    report.get("_l4_retry_candidate_ids")
                    or report.get("_l4_candidate_ids")
                    or ()
                )
                l4_report = report
                if l4_config_error:
                    l4_report["status"] = (
                        "applied_l4_degraded"
                        if l4_report.get("lanes_status") == "committed"
                        else "l4_config_error"
                    )
                    l4_report.setdefault("l4", {})["status"] = "config_error"
            else:
                candidate_ids = tuple(l4_retry_context.get("candidate_ids") or ())
                if l4_config_error:
                    l4_report = {
                        "ok": False,
                        "status": "l4_config_error",
                        "lanes_status": "not_run",
                        "l4": {"status": "config_error", "errors": 1},
                        "_l4_retry_candidate_ids": list(candidate_ids),
                    }
                elif candidate_ids:
                    assert llm_call is not None
                    l4_report = run_l4_retry(
                        provider._hermes_home,
                        provider._config,
                        llm_call=llm_call,
                        candidate_ids=candidate_ids,
                        scope_ids=writable_scope_ids,
                    )
                else:
                    l4_report = {
                        "ok": True,
                        "status": "l4_idle",
                        "lanes_status": "not_run",
                        "l4": {"status": "idle", "errors": 0},
                    }
                report = l4_report

            if l4_config_error:
                retry_ids = tuple(
                    l4_report.get("_l4_retry_candidate_ids") or candidate_ids
                )
            else:
                retry_ids = tuple(
                    l4_report.get("_l4_retry_candidate_ids") or ()
                )
            l4_status = str((l4_report.get("l4") or {}).get("status") or "")
            if (
                l4_config_error
                or retry_ids
                or l4_status not in {"ok", "idle"}
            ):
                finished = retry_adjudication_schedule(
                    db_path,
                    claim_token=l4_claim_token,
                    scheduled_at=time.time(),
                    trigger=f"{trigger}:l4",
                    interval_hours=interval_hours,
                    retry_after_seconds=retry_backoff_seconds,
                    target_id=l4_target_id,
                    retry_context={
                        "candidate_ids": list(retry_ids),
                        "reason": (
                            "l4_config_error"
                            if l4_config_error
                            else (l4_status or "not_run")
                        ),
                    },
                )
            else:
                finished = complete_adjudication_schedule(
                    db_path,
                    claim_token=l4_claim_token,
                    completed_at=time.time(),
                    trigger=f"{trigger}:l4",
                    interval_hours=interval_hours,
                    target_id=l4_target_id,
                )
            if not finished:
                assert report is not None
                report = {
                    **report,
                    "ok": False,
                    "status": "schedule_claim_lost",
                }
                if claim_token is not None:
                    release_adjudication_schedule(
                        db_path,
                        claim_token=claim_token,
                        released_at=time.time(),
                        trigger=trigger,
                        interval_hours=interval_hours,
                        target_id=target_id,
                    )
                provider._last_adjudication_report = {
                    key: value
                    for key, value in report.items()
                    if not key.startswith("_")
                }
                return

        if claim_token is not None:
            assert report is not None
            if lanes_committed:
                completed_at = time.time()
                finished = complete_adjudication_schedule(
                    db_path,
                    claim_token=claim_token,
                    completed_at=completed_at,
                    trigger=trigger,
                    interval_hours=interval_hours,
                    target_id=target_id,
                )
                if finished:
                    provider._last_adjudication_at = completed_at
            else:
                finished = retry_adjudication_schedule(
                    db_path,
                    claim_token=claim_token,
                    scheduled_at=time.time(),
                    trigger=trigger,
                    interval_hours=interval_hours,
                    retry_after_seconds=retry_backoff_seconds,
                    target_id=target_id,
                )
            if not finished:
                report = {
                    **report,
                    "ok": False,
                    "status": "schedule_claim_lost",
                }

        if report is None:
            return
        report = {key: value for key, value in report.items() if not key.startswith("_")}
        provider._last_adjudication_report = report
        logger.info(
            "Scope Recall auto adjudication after %s: %s",
            trigger,
            json.dumps(
                {
                    "status": report.get("status"),
                    "lanes": report.get("lanes"),
                    "l4": report.get("l4"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    except Exception:
        for token, owned_target, owned_trigger in (
            (claim_token, target_id, trigger),
            (l4_claim_token, l4_target_id, f"{trigger}:l4"),
        ):
            if token is None:
                continue
            try:
                release_adjudication_schedule(
                    db_path,
                    claim_token=token,
                    released_at=time.time(),
                    trigger=owned_trigger,
                    interval_hours=interval_hours,
                    target_id=owned_target,
                )
            except Exception:
                logger.exception("Scope Recall auto adjudication claim release failed")
        logger.exception("Scope Recall auto adjudication failed after %s", trigger)


__all__ = [
    "DEFAULT_CONFIG",
    "L4ConfigurationError",
    "build_l4_llm_call",
    "run_auto_adjudication",
    "run_l4_retry",
    "run_provider_auto_adjudication",
]
