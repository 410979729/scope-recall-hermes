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
  candidates is re-examined against their own journal evidence by an LLM.
  Supported claims promote, unsupported claims archive, uncertain claims stay
  and are retried on later runs; after ``l4_max_uncertain_rounds`` the row is
  archived as unresolvable instead of rotting forever.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .candidate_promotion import (
    candidate_rows,
    classify_candidate_row,
    load_metadata,
    now_iso,
)
from .candidate_review import transition_candidate_metadata
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
    "promote_min_age_hours": 24,
    "max_promotions_per_run": 100,
    "max_archives_per_run": 200,
    "l4_enabled": True,
    "l4_budget_per_run": 20,
    "l4_max_uncertain_rounds": 3,
    "l4_max_evidence_chars": 2400,
}

_L4_PROMPT = """你是记忆库的接地审计员。下面是一条候选长期记忆和它的原始证据（对话日志片段）。
请仅依据证据判断这条记忆是否成立。

候选记忆（target={target}, type={memory_type}）:
{content}

原始证据:
{evidence}

只输出一个 JSON 对象，不要输出其他内容：
{{"verdict": "supported" | "unsupported" | "uncertain", "reason": "不超过40字的理由"}}
判定标准：证据直接支持记忆的关键事实 -> supported；证据与记忆矛盾或完全无关 -> unsupported；证据不足以判断 -> uncertain。"""


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


def _journal_evidence(
    conn: sqlite3.Connection, memory_id: str, *, max_chars: int
) -> str:
    rows = conn.execute(
        """
        SELECT je.role, je.content
        FROM memory_journal_sources mjs
        JOIN journal_entries je ON je.id = mjs.journal_entry_id
        WHERE mjs.memory_id = ?
        ORDER BY je.id ASC
        LIMIT 6
        """,
        (str(memory_id),),
    ).fetchall()
    parts: list[str] = []
    used = 0
    for row in rows:
        snippet = sanitize_report_text(str(row["content"] or ""))
        if not snippet:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunk = f"[{row['role']}] {snippet[:remaining]}"
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)


def _parse_l4_verdict(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except ValueError:
        return "uncertain", "unparseable reviewer output"
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"supported", "unsupported", "uncertain"}:
        verdict = "uncertain"
    reason = sanitize_report_text(str(payload.get("reason") or ""))[:120]
    return verdict, reason


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


def _mark_uncertain_round(
    conn: sqlite3.Connection, row: sqlite3.Row, *, reason: str, at: str
) -> int:
    metadata = load_metadata(row["metadata"])
    rounds = int(metadata.get("l4_uncertain_rounds") or 0) + 1
    metadata["l4_uncertain_rounds"] = rounds
    metadata["l4_last_uncertain_at"] = at
    metadata["l4_last_uncertain_reason"] = reason
    cur = conn.execute(
        "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
        (
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            at,
            str(row["id"]),
            str(row["updated_at"] or ""),
        ),
    )
    if int(cur.rowcount or 0) != 1:
        return int(load_metadata(row["metadata"]).get("l4_uncertain_rounds") or 0)
    return rounds


def run_auto_adjudication(
    hermes_home: Path,
    runtime_config: dict[str, Any] | None = None,
    *,
    llm_call: Callable[[str], str] | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Run one bounded no-human adjudication pass and return the L5 summary."""

    raw = (runtime_config or {}).get("auto_adjudication")
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        config.update(raw)
    if not bool(config.get("enabled", True)):
        return {
            "ok": True,
            "status": "disabled",
            "lanes": {},
            "l4": {"enabled": False, "errors": 0, "exhausted_archived": 0},
        }

    db_path = memory_db_path(Path(hermes_home))
    if not db_path.exists():
        return {"ok": False, "status": "missing_database", "path": str(db_path)}

    batch_id = f"auto-adjudication-{uuid.uuid4().hex[:12]}"
    at = now_iso()
    promote_cap = _config_int(config, "max_promotions_per_run", 100)
    archive_cap = _config_int(config, "max_archives_per_run", 200)
    min_age_hours = float(config.get("promote_min_age_hours") or 24)
    l4_budget = _config_int(config, "l4_budget_per_run", 20)
    l4_max_rounds = max(1, _config_int(config, "l4_max_uncertain_rounds", 3))
    l4_evidence_chars = _config_int(config, "l4_max_evidence_chars", 2400)
    l4_enabled = bool(config.get("l4_enabled", True)) and llm_call is not None

    summary: dict[str, Any] = {
        "ok": True,
        "status": "applied",
        "batch_id": batch_id,
        "at": at,
        "lanes": {
            "promoted": 0,
            "promote_deferred_young": 0,
            "archived": 0,
            "held_for_l4": 0,
            "defer_recent": 0,
            "skipped": 0,
            "conflicts_skipped": 0,
        },
        "l4": {
            "enabled": l4_enabled,
            "reviewed": 0,
            "supported": 0,
            "unsupported": 0,
            "uncertain": 0,
            "exhausted_archived": 0,
            "errors": 0,
        },
        "exceptions": [],
    }

    try:
        with holding_truth_writer_lease(
            Path(hermes_home) / "scope-recall", role="auto_adjudication"
        ):
            conn = connect_memory_db(db_path, apply=True, timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.row_factory = sqlite3.Row
            try:
                ensure_governance_schema(conn)
                rows = candidate_rows(conn, scope_ids=None, limit=limit)
                l4_pool: list[sqlite3.Row] = []
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
                        else:
                            summary["lanes"]["conflicts_skipped"] += 1
                    elif decision.lane == "defer_recent":
                        summary["lanes"]["defer_recent"] += 1
                    elif decision.lane == "skip":
                        summary["lanes"]["skipped"] += 1
                    else:
                        summary["lanes"]["held_for_l4"] += 1
                        l4_pool.append(row)

                if l4_enabled and l4_pool:
                    l4_pool.sort(key=lambda row: str(row["updated_at"] or ""))
                    for row in l4_pool[:l4_budget]:
                        try:
                            evidence = _journal_evidence(
                                conn, str(row["id"]), max_chars=l4_evidence_chars
                            )
                        except sqlite3.OperationalError as exc:
                            summary["l4"]["errors"] += 1
                            summary["exceptions"].append(
                                {
                                    "kind": "l4_evidence_lookup",
                                    "id": str(row["id"]),
                                    "error": sanitize_report_text(str(exc))[:160],
                                }
                            )
                            continue
                        if not evidence:
                            verdict, reason = "uncertain", "no journal evidence linked"
                        else:
                            prompt = _L4_PROMPT.format(
                                target=str(row["target"] or ""),
                                memory_type=str(
                                    load_metadata(row["metadata"]).get("memory_type") or ""
                                ),
                                content=sanitize_report_text(str(row["content"] or ""))[:1200],
                                evidence=evidence,
                            )
                            try:
                                conn.commit()
                                prepare_network_boundary(
                                    conn, "auto_adjudication.l4_llm"
                                )
                                verdict, reason = _parse_l4_verdict(llm_call(prompt))  # type: ignore[reportOptionalCall]
                            except Exception as exc:
                                summary["l4"]["errors"] += 1
                                summary["exceptions"].append(
                                    {
                                        "kind": "l4_llm_error",
                                        "id": str(row["id"]),
                                        "error": sanitize_report_text(str(exc))[:160],
                                    }
                                )
                                continue
                        summary["l4"]["reviewed"] += 1
                        summary["l4"][verdict] += 1
                        if verdict == "supported":
                            _transition(
                                conn,
                                row,
                                action="promote",
                                reason=f"l4_grounded:{reason}",
                                batch_id=batch_id,
                                at=at,
                            )
                        elif verdict == "unsupported":
                            _transition(
                                conn,
                                row,
                                action="archive",
                                reason=f"l4_ungrounded:{reason}",
                                batch_id=batch_id,
                                at=at,
                            )
                        else:
                            rounds = _mark_uncertain_round(conn, row, reason=reason, at=at)
                            if rounds >= l4_max_rounds:
                                fresh = conn.execute(
                                    "SELECT * FROM memories WHERE id = ?",
                                    (str(row["id"]),),
                                ).fetchone()
                                if fresh is not None:
                                    _transition(
                                        conn,
                                        fresh,
                                        action="archive",
                                        reason=f"l4_unresolvable_after_{rounds}_rounds",
                                        batch_id=batch_id,
                                        at=at,
                                    )
                                    summary["l4"]["exhausted_archived"] += 1
                conn.commit()
            except Exception as exc:
                conn.rollback()
                summary["ok"] = False
                summary["status"] = "failed"
                summary["error"] = sanitize_report_text(str(exc))[:300]
                logger.exception("Scope Recall auto adjudication failed")
            finally:
                conn.close()
    except TruthWriterBusyError:
        summary["ok"] = False
        summary["status"] = "truth_writer_busy"
    return summary


def build_l4_llm_call(
    hermes_home: Path, journal_config: dict[str, Any]
) -> Callable[[str], str] | None:
    """Build the grounded-review LLM callable from the digest LLM settings.

    L4 reuses the journal digest provider/model (the same trusted extraction
    channel); if that resolution fails the caller degrades to lanes-only
    adjudication instead of blocking the run.
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
    except Exception:
        logger.exception(
            "Scope Recall L4 grounded review is unavailable: digest LLM config "
            "did not resolve; adjudication continues lanes-only"
        )
        return None

    def call(prompt: str) -> str:
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
        )

    return call


def run_provider_auto_adjudication(provider: Any, *, trigger: str) -> None:
    """Run the scheduled adjudication pass from the digest worker."""

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
    now = time.time()
    if provider._last_adjudication_at and now - provider._last_adjudication_at < interval_hours * 3600:
        return
    provider._last_adjudication_at = now
    try:
        llm_call = None
        if config_bool(adjudication_config, "l4_enabled", True):
            llm_call = build_l4_llm_call(provider._hermes_home, provider._journal_config())
        report = run_auto_adjudication(
            provider._hermes_home,
            provider._config,
            llm_call=llm_call,
        )
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
        logger.exception("Scope Recall auto adjudication failed after %s", trigger)


__all__ = [
    "DEFAULT_CONFIG",
    "build_l4_llm_call",
    "run_auto_adjudication",
    "run_provider_auto_adjudication",
]
