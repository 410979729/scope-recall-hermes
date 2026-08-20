"""Compatibility facade and orchestration layer for journal capture, digest, and recovery helpers.

Many public imports historically pointed here, so split-out modules are re-exported while preserving old monkeypatch surfaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re  # noqa: F401
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .capture_filters import sanitize_report_text, sanitize_structured_value, should_capture_text  # noqa: F401
from .digest_pollution import assess_digest_batch
from .digest_run_results import journal_digest_metadata, journal_digest_receipt_fields, journal_digest_success_result, no_unprocessed_journal_result
from .fact_actions import EvolutionAction
from .fact_evolution import (
    execute_pipeline_proposal,
    fact_evolution_enabled,
    memory_type_uses_fact_evolution,
)
from .gating import clean_text, compact_text, dedup_key  # noqa: F401
from .governance import semantic_similarity
from .http_utils import explicit_insecure_endpoint_opt_in
from .maintenance_lease import install_activation_lease_authorizer
from .models import RuntimeScope, recall_scope_id_for_target
from .truth_connection import connect_truth_database
from .write_kernel import prepare_network_boundary, release_snapshot_transaction
from .digest_state import (
    LeavePlan,
    active_journal_digest_llm_error,
    admission_leave_reason,
    attach_digestible_tool_provenance,
    leave_plan_receipt_actions,
    loaded_leave_sets,
    plan_loaded_leave,
    split_digestible_entries,
)
from .writer_lease import TruthWriterBusyError, TruthWriterLease
from .lifecycle_policy import durable_lifecycle_visible_sql
from .nightly_digest import call_llm
from .journal_candidates import (
    JournalDigestCandidate,
    _classify_target_and_type,
    _digest_role_summary,
    _DOMAIN_TOPIC_HINTS,
    _entry_entities,
    _GENERIC_TOPIC_ENTITIES,
    _heuristic_candidate_content,
    _looks_like_historical_template_noise,
    _segment_session_entries,
    _topic_entities,
    _topic_label,
    _topic_signature,
    _topic_tags,
    _unique,
    candidate_metadata,
    heuristic_journal_candidates,
)
from .journal_llm import (
    JournalDigestLLMError,
    _call_llm_with_retries,
    _classify_llm_digest_error,
    _quarantine_classification,
)
from .journal_extractors import (
    _coerce_nonnegative_float,
    _coerce_positive_int,
    _config_bool,
    _journal_from_digest_candidate,
    _journal_runtime_config,
    _journal_session_bundles,
    _parse_entry_timestamp,
    _runtime_config,
    llm_journal_candidates,
)
from .journal_store import (
    BASE64ISH_RE,
    DATA_URL_PREFIX_RE,
    JournalEntry,
    _chunk_journal_text,
    _insert_journal_entry,
    _journal_capture_allowed,
    _journal_entry_for_digest,
    _journal_unprocessed_count,
    _looks_like_base64_blob,
    _metadata_json,
    _prune_processed_journal,
    _row_to_entry,
    _strip_inline_data_urls,
    append_journal_entry,
    clear_current_deferral,
    ensure_journal_schema,
    increment_extraction_attempts,
    increment_retryable_failures,
    reset_retryable_failures,
    load_unprocessed_journal_entries,
    mark_entries_deferred,
    mark_entries_processed,
    advance_session_digest_cursors,
)
from .scope import accessible_scope_ids, build_scope_id, build_shared_scope_id, canonical_user_id, normalize_scope_identity, writable_scope_ids
from .source_isolation import memory_isolated_chat_ids, scope_is_memory_isolated
from .sqlite_recovery import rollback_if_active
from .sql_store import ensure_schema, now_iso, store_row
from .vector_runtime import (
    replay_vector_outbox,
    replay_vector_outbox_events,
    vector_write_replay_limit,
)

logger = logging.getLogger(__name__)

# Compatibility surface: tests and operator probes historically monkeypatch
# ``scope_recall.journal.call_llm`` before calling the journal retry helper.
# ``journal_llm._active_call_llm`` checks this module attribute dynamically.
_JOURNAL_CALL_LLM_COMPAT = call_llm
# Compatibility re-exports: old imports such as ``scope_recall.journal.JournalDigestLLMError``
# and ``scope_recall.journal._classify_llm_digest_error`` must remain module attributes.
_JOURNAL_LLM_REEXPORT_COMPAT = (
    JournalDigestLLMError,
    _call_llm_with_retries,
    _classify_llm_digest_error,
    _quarantine_classification,
)
# Compatibility re-exports for H4 journal storage/capture split. These symbols
# historically lived in ``scope_recall.journal`` and external tests/operators
# still import or monkeypatch them from that module.
_JOURNAL_STORE_REEXPORT_COMPAT = (
    BASE64ISH_RE,
    DATA_URL_PREFIX_RE,
    JournalEntry,
    _chunk_journal_text,
    _insert_journal_entry,
    _journal_capture_allowed,
    _journal_entry_for_digest,
    _journal_unprocessed_count,
    _looks_like_base64_blob,
    _metadata_json,
    _prune_processed_journal,
    _row_to_entry,
    _strip_inline_data_urls,
    append_journal_entry,
    ensure_journal_schema,
    load_unprocessed_journal_entries,
    mark_entries_processed,
)
# Compatibility re-exports for H5 journal candidate/heuristic split.
_JOURNAL_CANDIDATES_REEXPORT_COMPAT = (
    JournalDigestCandidate,
    _classify_target_and_type,
    _digest_role_summary,
    _DOMAIN_TOPIC_HINTS,
    _entry_entities,
    _GENERIC_TOPIC_ENTITIES,
    _heuristic_candidate_content,
    _looks_like_historical_template_noise,
    _segment_session_entries,
    _topic_entities,
    _topic_label,
    _topic_signature,
    _topic_tags,
    _unique,
    candidate_metadata,
    heuristic_journal_candidates,
)
# Compatibility re-exports for H6 journal LLM extractor/session-bundle split.
_JOURNAL_EXTRACTORS_REEXPORT_COMPAT = (
    _coerce_nonnegative_float,
    _coerce_positive_int,
    _config_bool,
    _journal_from_digest_candidate,
    _journal_runtime_config,
    _journal_session_bundles,
    _parse_entry_timestamp,
    _runtime_config,
    llm_journal_candidates,
)

from .journal_admission import (  # noqa: E402
    EPHEMERAL_RELEASE_STATE_RE,  # noqa: F401
    HIGH_VALUE_DURABLE_SIGNAL_RE,  # noqa: F401
    JOURNAL_TARGETS,  # noqa: F401
    LOW_VALUE_LOG_RE,  # noqa: F401
    LOW_VALUE_NOTIFICATION_RE,  # noqa: F401
    LOW_VALUE_PROGRESS_RE,  # noqa: F401
    TRANSIENT_PHASE_GATE_RE,  # noqa: F401
    _candidate_allowed,  # noqa: F401
    _candidate_rejection_reason,
    _has_high_value_durable_signal,  # noqa: F401
    _low_value_promotion_reason,  # noqa: F401
)



from .journal_match_policy import (  # noqa: E402
    _WORKFLOW_CONTINUATION_TOKENS,  # noqa: F401
    _is_workflow_continuation,
    _metadata_entities,
    _workflow_continuation_tokens,
)


def _find_match(conn: sqlite3.Connection, scope_ids: list[str], candidate: JournalDigestCandidate) -> tuple[str, str, float]:
    placeholders = ",".join("?" for _ in scope_ids)
    rows = conn.execute(
        f"""
        SELECT m.id, m.content, m.metadata
        FROM memories AS m
        WHERE m.scope_id IN ({placeholders})
          AND m.target = ?
          AND {durable_lifecycle_visible_sql('m')}
        ORDER BY m.updated_at DESC
        LIMIT 300
        """,
        [*scope_ids, candidate.target],
    ).fetchall()
    best_id = ""
    best_content = ""
    best_score = 0.0
    candidate_key = dedup_key(candidate.content)
    candidate_entities = set(candidate.entities)
    candidate_tags = set(candidate.tags)
    candidate_topic_tags = {tag for tag in candidate_tags if tag.startswith("topic:")}
    candidate_session_tags = {tag for tag in candidate_tags if tag.startswith("session:")}
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except Exception:
            metadata = {}
        content = str(row["content"])
        if dedup_key(content) == candidate_key:
            return str(row["id"]), content, 1.0
        score = semantic_similarity(content, candidate.content)
        existing_tags = set(str(tag) for tag in metadata.get("tags", []) if str(tag).strip()) if isinstance(metadata, dict) else set()
        existing_entities = _metadata_entities(metadata)
        existing_topic_tags = {tag for tag in existing_tags if tag.startswith("topic:")}
        existing_session_tags = {tag for tag in existing_tags if tag.startswith("session:")}
        same_session = bool(candidate_session_tags & existing_session_tags)
        same_topic = bool(candidate_topic_tags & existing_topic_tags)
        candidate_workflow_tokens = _workflow_continuation_tokens(candidate.content, candidate_tags, candidate_entities)
        existing_workflow_tokens = _workflow_continuation_tokens(content, existing_tags, existing_entities)
        workflow_continuation = _is_workflow_continuation(candidate_workflow_tokens, existing_workflow_tokens)
        lower = content.lower()
        entity_hits = sum(1 for entity in candidate_entities if entity and entity in lower)
        tag_hits = sum(1 for tag in candidate_tags if tag and tag in lower)
        score = max(score, min(0.86, score + entity_hits * 0.08 + tag_hits * 0.04))
        if same_session and (same_topic or workflow_continuation):
            score = max(score, 0.58)
        elif same_topic:
            score = max(score, 0.56)
        elif candidate_topic_tags and existing_topic_tags:
            score = min(score, 0.52)
        if score > best_score:
            best_id = str(row["id"])
            best_content = content
            best_score = score
    return best_id, best_content, best_score


def _memory_scope_id(conn: sqlite3.Connection, memory_id: str) -> str:
    row = conn.execute("SELECT scope_id FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return str(row["scope_id"] if row is not None else "")


def _record_journal_sources(conn: sqlite3.Connection, *, memory_id: str, run_id: str, entry_ids: list[int]) -> None:
    now = now_iso()
    conn.executemany(
        """
        INSERT OR REPLACE INTO memory_journal_sources(memory_id, journal_entry_id, run_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [(memory_id, int(entry_id), run_id, now) for entry_id in entry_ids],
    )


def _record_journal_rejection(conn: sqlite3.Connection, *, run_id: str, entry_ids: list[int], reason: str, candidate: JournalDigestCandidate) -> None:
    now = now_iso()
    snippet = compact_text(sanitize_report_text(candidate.content), 500)
    conn.executemany(
        """
        INSERT OR REPLACE INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(int(entry_id), run_id, reason, snippet, now) for entry_id in entry_ids],
    )


def _quarantine_journal_entries(conn: sqlite3.Connection, *, run_id: str, entries: list[JournalEntry], reason: str, error: Exception) -> None:
    entry_ids = [int(entry.id) for entry in entries]
    _record_journal_rejection(
        conn,
        run_id=run_id,
        entry_ids=entry_ids,
        reason=reason,
        candidate=JournalDigestCandidate(
            content=sanitize_report_text(f"{reason}: {type(error).__name__}: {str(error)[:400]}"),
            target="memory",
            entry_ids=entry_ids,
        ),
    )


def _merge_metadata(conn: sqlite3.Connection, *, memory_id: str, candidate: JournalDigestCandidate, run_id: str) -> None:
    from .graph import load_metadata, sync_memory_entities

    row = conn.execute("SELECT content, target, metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return
    existing = load_metadata(row["metadata"])
    incoming = candidate_metadata(candidate, run_id)
    for key in ("entities", "tags", "journal_entry_ids", "journal_session_ids"):
        existing_value = existing.get(key)
        incoming_value = incoming.get(key)
        existing_values: list[Any] = existing_value if isinstance(existing_value, list) else []
        incoming_values: list[Any] = incoming_value if isinstance(incoming_value, list) else []
        merged = _unique([*map(str, existing_values), *map(str, incoming_values)], limit=240 if key == "journal_entry_ids" else 40)
        if merged:
            existing[key] = merged
    for key in ("journal_run_id", "journal_reason", "memory_type"):
        if incoming.get(key):
            existing[key] = incoming[key]
    existing["importance"] = max(float(existing.get("importance") or 0.0), float(incoming.get("importance") or 0.0))
    existing["confidence"] = max(float(existing.get("confidence") or 0.0), float(incoming.get("confidence") or 0.0))
    safe_metadata, _ = sanitize_structured_value(existing)
    existing = safe_metadata if isinstance(safe_metadata, dict) else {}
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(existing, ensure_ascii=False, sort_keys=True), memory_id))
    sync_memory_entities(conn, memory_id=memory_id, content=str(row["content"]), target=str(row["target"]), metadata=existing)



def _cross_platform_metadata(scope: RuntimeScope, config: dict[str, Any] | None = None) -> dict[str, Any]:
    canonical = canonical_user_id(scope, config)
    metadata = {"raw_platform": scope.platform or "cli", "raw_user_id": scope.user_id or "local"}
    if canonical:
        metadata["canonical_user"] = canonical
        metadata["scope_identity_mode"] = "canonical"
    return metadata


def _journal_candidate_uses_fact_evolution(
    candidate: JournalDigestCandidate,
    runtime_config: Mapping[str, Any] | None,
) -> bool:
    return (
        fact_evolution_enabled(runtime_config)
        and candidate.evolution is not None
        and memory_type_uses_fact_evolution(candidate.memory_type)
    )


def _apply_structured_journal_fact(
    conn: sqlite3.Connection,
    *,
    scope: RuntimeScope,
    local_scope_id: str,
    shared_scope_id: str,
    write_scope_ids: list[str],
    run_id: str,
    candidate: JournalDigestCandidate,
    dry_run: bool,
    runtime_config: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    proposal = candidate.evolution
    if proposal is None:
        raise ValueError("structured journal fact requires an evolution proposal")
    metadata = candidate_metadata(
        candidate,
        run_id,
        default_lifecycle=str(
            (runtime_config or {}).get("automatic_digest_default_lifecycle")
            or "candidate"
        ),
    )
    metadata.pop("fact_evolution", None)
    metadata["fact_evolution_action"] = proposal.action.value
    source_key = ":".join(
        [
            ",".join(str(item) for item in candidate.entry_ids[:200]),
            ",".join(candidate.session_ids[:40]),
            hashlib.sha256(candidate.content.encode("utf-8")).hexdigest(),
        ]
    )
    provenance_refs = [
        {
            "source_type": "journal_entry",
            "source_ref": str(entry_id),
            "metadata": {"journal_run_id": run_id},
        }
        for entry_id in candidate.entry_ids[:200]
    ]
    result = execute_pipeline_proposal(
        conn,
        proposal=proposal,
        lane="journal",
        run_id=run_id,
        source_key=source_key,
        trusted_scope_id=recall_scope_id_for_target(
            candidate.target,
            local_scope_id=local_scope_id,
            shared_scope_id=shared_scope_id,
            source="journal-digest",
        ),
        writable_scope_ids=write_scope_ids,
        actor="scope-recall-journal-digest",
        source="journal-digest",
        target=candidate.target,
        content=candidate.content,
        metadata={**_cross_platform_metadata(scope, runtime_config), **metadata},
        runtime_config=runtime_config,
        dry_run=dry_run,
        provenance_refs=provenance_refs,
        session_id=",".join(candidate.session_ids[:3]),
        platform=scope.platform,
        user_id=scope.user_id,
        chat_id=scope.chat_id,
        thread_id=scope.thread_id,
        gateway_session_key=scope.gateway_session_key,
        agent_identity=scope.agent_identity,
        agent_workspace=scope.agent_workspace,
    )
    receipt = result.receipt if isinstance(result.receipt, dict) else {}
    action: dict[str, Any] = {
        "evolution_action": result.action.value,
        "requested_action": proposal.action.value,
        "status": result.status,
        "action_id": result.action_id,
        "entry_ids": candidate.entry_ids,
    }
    for key in ("memory_ids", "claim_ids", "reason_codes"):
        if receipt.get(key):
            action[key] = receipt[key]
    if result.status == "preview":
        action["action"] = "preview"
        counter_key = "previewed"
    elif result.status == "review":
        action["action"] = "review"
        counter_key = "review"
    elif result.status == "blocked":
        action["action"] = "blocked"
        counter_key = "blocked"
    elif result.status == "noop":
        action["action"] = "skip"
        counter_key = "skipped"
    elif result.status == "replayed":
        action["action"] = "replay"
        return "replayed", action
    else:
        action["action"] = "evolve"
        counter_key = {
            EvolutionAction.ADD: "inserted",
            EvolutionAction.ENRICH: "enriched",
            EvolutionAction.SUPERSEDE: "superseded",
            EvolutionAction.RETRACT: "retracted",
        }.get(result.action, "applied")
        return counter_key, action

    if result.status in {"preview", "review"}:
        # Keep the source journal entries pending as the recoverable review
        # queue. A later explicitly-enabled apply run can revisit the same
        # evidence; preview/review is not a rejection and must not consume it.
        return counter_key, action
    if not dry_run:
        _record_journal_rejection(
            conn,
            run_id=run_id,
            entry_ids=candidate.entry_ids,
            reason=f"fact evolution {result.status}",
            candidate=candidate,
        )
    return counter_key, action


def _apply_structured_journal_fact_atomically(
    conn: sqlite3.Connection,
    *,
    scope: RuntimeScope,
    local_scope_id: str,
    shared_scope_id: str,
    write_scope_ids: list[str],
    run_id: str,
    candidate: JournalDigestCandidate,
    dry_run: bool,
    runtime_config: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], bool]:
    """Atomically bind one fact action/receipt to its source checkpoint."""

    if dry_run:
        counter_key, action = _apply_structured_journal_fact(
            conn,
            scope=scope,
            local_scope_id=local_scope_id,
            shared_scope_id=shared_scope_id,
            write_scope_ids=write_scope_ids,
            run_id=run_id,
            candidate=candidate,
            dry_run=True,
            runtime_config=runtime_config,
        )
        return counter_key, action, False

    from .transaction_guard import TruthTransactionTimer

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    fact_transaction_timer = TruthTransactionTimer("journal fact apply")
    token = hashlib.sha256(
        f"{run_id}:{','.join(str(item) for item in candidate.entry_ids)}".encode("utf-8")
    ).hexdigest()[:16]
    savepoint = f"journal_fact_{token}"
    conn.execute(f"SAVEPOINT {savepoint}")
    savepoint_active = True
    try:
        counter_key, action = _apply_structured_journal_fact(
            conn,
            scope=scope,
            local_scope_id=local_scope_id,
            shared_scope_id=shared_scope_id,
            write_scope_ids=write_scope_ids,
            run_id=run_id,
            candidate=candidate,
            dry_run=False,
            runtime_config=runtime_config,
        )
        consume_source = action.get("action") not in {"preview", "review"}
        if consume_source:
            mark_entries_processed(
                conn,
                entry_ids=[int(entry_id) for entry_id in candidate.entry_ids],
                run_id=run_id,
                commit=False,
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        savepoint_active = False
        if started_outer_transaction:
            conn.commit()
            if action.get("status") == "applied_pending_outer_commit":
                action["status"] = "applied"
        return counter_key, action, consume_source
    except Exception:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction:
            conn.rollback()
        raise
    finally:
        fact_transaction_timer.stop()


def _journal_candidate_components(
    candidates: list[JournalDigestCandidate],
) -> tuple[dict[int, tuple[int, ...]], dict[int, int]]:
    """Build connected components for every candidate sharing source entries."""

    eligible = set(range(len(candidates)))
    components: dict[int, tuple[int, ...]] = {}
    member_to_leader: dict[int, int] = {}
    while eligible:
        seed = min(eligible)
        component = {seed}
        entry_ids = {int(item) for item in candidates[seed].entry_ids}
        changed = True
        while changed:
            changed = False
            for index in sorted(eligible - component):
                candidate_entries = {
                    int(item) for item in candidates[index].entry_ids
                }
                if entry_ids.intersection(candidate_entries):
                    component.add(index)
                    entry_ids.update(candidate_entries)
                    changed = True
        eligible.difference_update(component)
        ordered = tuple(sorted(component))
        if len(ordered) < 2:
            continue
        leader = ordered[0]
        components[leader] = ordered
        for index in ordered:
            member_to_leader[index] = leader
    return components, member_to_leader


def _apply_structured_journal_fact_component_atomically(
    conn: sqlite3.Connection,
    *,
    scope: RuntimeScope,
    local_scope_id: str,
    shared_scope_id: str,
    write_scope_ids: list[str],
    run_id: str,
    candidates: list[JournalDigestCandidate],
    pollution_assessments: list[Any],
    dry_run: bool,
    runtime_config: dict[str, Any] | None,
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Apply one overlapping source-entry closure under a single transaction."""

    if dry_run:
        results = []
        for candidate, pollution in zip(
            candidates,
            pollution_assessments,
            strict=True,
        ):
            if pollution.quarantined:
                results.append(
                    (
                        "quarantined",
                        {
                            "action": "quarantine",
                            "reason_codes": list(pollution.reason_codes),
                            "entry_ids": candidate.entry_ids,
                        },
                    )
                )
                continue
            counter_key, action, _ = _apply_structured_journal_fact_atomically(
                conn,
                scope=scope,
                local_scope_id=local_scope_id,
                shared_scope_id=shared_scope_id,
                write_scope_ids=write_scope_ids,
                run_id=run_id,
                candidate=candidate,
                dry_run=True,
                runtime_config=runtime_config,
            )
            results.append((counter_key, action))
        return results, False

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    component_token = hashlib.sha256(
        (
            run_id
            + ":"
            + ",".join(
                str(entry_id)
                for entry_id in sorted(
                    {
                        int(entry_id)
                        for candidate in candidates
                        for entry_id in candidate.entry_ids
                    }
                )
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    savepoint = f"journal_fact_closure_{component_token}"
    conn.execute(f"SAVEPOINT {savepoint}")
    savepoint_active = True
    try:
        tentative: list[tuple[str, dict[str, Any], bool]] = []
        for candidate, pollution in zip(
            candidates,
            pollution_assessments,
            strict=True,
        ):
            if pollution.quarantined:
                _record_journal_rejection(
                    conn,
                    run_id=run_id,
                    entry_ids=candidate.entry_ids,
                    reason=(
                        "digest pollution: " + ",".join(pollution.reason_codes)
                    ),
                    candidate=candidate,
                )
                tentative.append(
                    (
                        "quarantined",
                        {
                            "action": "quarantine",
                            "reason_codes": list(pollution.reason_codes),
                            "entry_ids": candidate.entry_ids,
                        },
                        True,
                    )
                )
                continue
            tentative.append(
                _apply_structured_journal_fact_atomically(
                    conn,
                    scope=scope,
                    local_scope_id=local_scope_id,
                    shared_scope_id=shared_scope_id,
                    write_scope_ids=write_scope_ids,
                    run_id=run_id,
                    candidate=candidate,
                    dry_run=False,
                    runtime_config=runtime_config,
                )
            )
        closure_consumed = all(item[2] for item in tentative)
        if not closure_consumed:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_active = False
            if started_outer_transaction:
                conn.rollback()
            results: list[tuple[str, dict[str, Any]]] = []
            for counter_key, action, consumed in tentative:
                if consumed:
                    action = {
                        **action,
                        "action": "review",
                        "status": "closure_pending",
                        "applied": False,
                        "reason": "source entry candidate closure is not fully consumable",
                    }
                    counter_key = "review"
                results.append((counter_key, action))
            return results, False

        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        savepoint_active = False
        if started_outer_transaction:
            conn.commit()
        results = []
        for counter_key, action, _consumed in tentative:
            if action.get("status") == "applied_pending_outer_commit":
                action["status"] = "applied"
            results.append((counter_key, action))
        return results, True
    except Exception:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction and conn.in_transaction:
            conn.rollback()
        raise


def _latest_current_vector_event_id(
    conn: sqlite3.Connection,
    memory_id: str,
) -> int:
    """Return the newest causal event for one memory in the current generation."""

    try:
        row = conn.execute(
            """
            SELECT id
            FROM vector_outbox
            WHERE memory_id = ?
              AND generation_id = (
                  SELECT value FROM vector_generation_state
                  WHERE key = 'current_generation'
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(memory_id),),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return 0
        raise
    return int(row[0]) if row is not None else 0


def _replay_or_defer_journal_vector(
    vector_runtime: Any,
    deferred_vector_ops: list[dict[str, Any]] | None,
    payload: dict[str, Any],
) -> dict[str, int]:
    if vector_runtime is None:
        return {"claimed": 0, "completed": 0, "failed": 0}
    if deferred_vector_ops is not None:
        deferred_vector_ops.append(payload)
        return {"claimed": 0, "completed": 0, "failed": 0}
    event_id = int(payload.get("event_id") or 0)
    if event_id > 0:
        return replay_vector_outbox_events(
            vector_runtime,
            event_ids=[event_id],
        )
    return replay_vector_outbox(
        vector_runtime,
        limit=vector_write_replay_limit(vector_runtime),
    )


def _drain_deferred_journal_vector(
    vector_runtime: Any,
    deferred_vector_ops: list[dict[str, Any]],
) -> dict[str, int]:
    totals = {"claimed": 0, "completed": 0, "failed": 0}
    for payload in deferred_vector_ops:
        part = _replay_or_defer_journal_vector(vector_runtime, None, payload)
        for key in totals:
            totals[key] += int(part.get(key) or 0)
    return totals


def _commit_truth_then_drain_vector(
    conn: sqlite3.Connection,
    vector_runtime: Any,
    deferred_vector_ops: list[dict[str, Any]] | None,
    *,
    owns_transaction: bool = True,
) -> dict[str, int]:
    """Commit the truth UoW, then drain deferred vector/network work.

    When the caller already owns the SQLite transaction, this helper must not
    commit or treat derived vector drain as durable. The pending-outer-commit
    contract stays with that caller.
    """

    if not owns_transaction:
        return {"claimed": 0, "completed": 0, "failed": 0}
    if conn.in_transaction:
        conn.commit()
    if not deferred_vector_ops:
        return {"claimed": 0, "completed": 0, "failed": 0}
    return _drain_deferred_journal_vector(vector_runtime, deferred_vector_ops)


def _apply_mixed_journal_component_atomically(
    conn: sqlite3.Connection,
    vector_runtime: Any,
    scope: RuntimeScope,
    *,
    run_id: str,
    candidates: list[JournalDigestCandidate],
    dry_run: bool,
    runtime_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply a mixed/legacy source-entry closure under one SQLite transaction."""

    if dry_run:
        return apply_journal_candidates(
            conn,
            vector_runtime,
            scope,
            run_id=run_id,
            candidates=candidates,
            dry_run=True,
            runtime_config=runtime_config,
            _skip_components=True,
        )

    from .transaction_guard import TruthTransactionTimer

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    transaction_timer = TruthTransactionTimer(
        f"journal mixed component apply ({len(candidates)} candidates)"
    )
    component_token = hashlib.sha256(
        (
            run_id
            + ":mixed:"
            + ",".join(
                str(entry_id)
                for entry_id in sorted(
                    {
                        int(entry_id)
                        for candidate in candidates
                        for entry_id in candidate.entry_ids
                    }
                )
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    savepoint = f"journal_mixed_closure_{component_token}"
    conn.execute(f"SAVEPOINT {savepoint}")
    savepoint_active = True
    deferred_vector_ops: list[dict[str, Any]] = []
    try:
        result = apply_journal_candidates(
            conn,
            vector_runtime,
            scope,
            run_id=run_id,
            candidates=candidates,
            dry_run=False,
            runtime_config=runtime_config,
            _skip_components=True,
            _defer_commits=True,
            _deferred_vector_ops=deferred_vector_ops,
        )
        expected_entry_ids = {
            int(entry_id)
            for candidate in candidates
            for entry_id in candidate.entry_ids
        }
        processed_entry_ids = {
            int(entry_id) for entry_id in result["processed_entry_ids"]
        }
        pollution_entry_ids = {
            int(entry_id) for entry_id in result.get("pollution_entry_ids") or []
        }
        if processed_entry_ids | pollution_entry_ids != expected_entry_ids:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_active = False
            if started_outer_transaction:
                conn.rollback()
            return {
                "counts": {"review": len(candidates)},
                "pollution_counts": {},
                "actions": [
                    {
                        "action": "review",
                        "status": "closure_pending",
                        "applied": False,
                        "reason": (
                            "source entry candidate closure is not fully consumable"
                        ),
                        "entry_ids": list(candidate.entry_ids),
                    }
                    for candidate in candidates
                ],
                "processed_entry_ids": [],
                "pollution_entry_ids": [],
            }
        mark_entries_processed(
            conn,
            entry_ids=sorted(processed_entry_ids),
            run_id=run_id,
            commit=False,
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        savepoint_active = False
        if started_outer_transaction:
            conn.commit()
            for action in result["actions"]:
                if action.get("status") == "applied_pending_outer_commit":
                    action["status"] = "applied"
            if deferred_vector_ops:
                event_ids = [
                    int(payload.get("event_id") or 0)
                    for payload in deferred_vector_ops
                    if int(payload.get("event_id") or 0) > 0
                ]
                result["vector_replay"] = replay_vector_outbox_events(
                    vector_runtime,
                    event_ids=event_ids,
                )
        return result
    except Exception:
        if savepoint_active and conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        transaction_timer.stop()


def _journal_candidate_source_evidence(
    conn: sqlite3.Connection,
    candidates: list[JournalDigestCandidate],
) -> dict[str, list[str]]:
    """Load only referenced journal text for deterministic admission checks."""

    entry_ids = sorted(
        {
            int(entry_id)
            for candidate in candidates
            for entry_id in candidate.entry_ids
        }
    )
    evidence: dict[str, list[str]] = {}
    for offset in range(0, len(entry_ids), 500):
        chunk = entry_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT session_id, content FROM journal_entries WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        for session_id, content in rows:
            clean_session_id = str(session_id or "")
            clean_content = str(content or "")
            if clean_content:
                evidence.setdefault(clean_session_id, []).append(clean_content)
    return {
        session_id: list(dict.fromkeys(items))
        for session_id, items in evidence.items()
    }


def _run_apply_unit(conn: sqlite3.Connection, index: int, enabled: bool, fn):
    if not enabled:
        return fn()
    name = f"apply_c{index}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        result = fn()
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")
    return result


def apply_journal_candidates(
    conn: sqlite3.Connection,
    vector_runtime: Any,
    scope: RuntimeScope,
    *,
    run_id: str,
    candidates: list[JournalDigestCandidate],
    dry_run: bool = False,
    runtime_config: dict[str, Any] | None = None,
    _skip_components: bool = False,
    _defer_commits: bool = False,
    _deferred_vector_ops: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Store journal digest candidates and mark processed entries only after successful handling.

    This ordering prevents a failed write from silently advancing the journal watermark and losing evidence."""
    caller_owns_transaction = bool(getattr(conn, "in_transaction", False))
    owns_external_drain = _deferred_vector_ops is None and not dry_run
    owned_deferred: list[dict[str, Any]] = []
    if owns_external_drain:
        _deferred_vector_ops = owned_deferred
        _defer_commits = True
    scope = normalize_scope_identity(scope, runtime_config)
    local_scope_id = build_scope_id(scope, runtime_config)
    shared_scope_id = build_shared_scope_id(scope, runtime_config)
    write_scope_ids = writable_scope_ids(scope, runtime_config)
    counts = Counter()
    pollution_counts = Counter()
    actions: list[dict[str, Any]] = []
    processed_entry_ids: set[int] = set()
    pollution_entry_ids: set[int] = set()
    pollution_assessments = assess_digest_batch(
        candidates,
        batch_evidence=_journal_candidate_source_evidence(conn, candidates),
    )
    if _skip_components:
        candidate_components: dict[int, tuple[int, ...]] = {}
        candidate_component_members: dict[int, int] = {}
    else:
        candidate_components, candidate_component_members = (
            _journal_candidate_components(candidates)
        )
    for candidate_index, (candidate, pollution) in enumerate(
        zip(candidates, pollution_assessments, strict=True)
    ):
        component_leader = candidate_component_members.get(candidate_index)
        if component_leader is not None:
            if component_leader != candidate_index:
                continue
            component_indexes = candidate_components[component_leader]
            component_candidates = [candidates[index] for index in component_indexes]
            component_pollution = [
                pollution_assessments[index] for index in component_indexes
            ]
            all_fact_or_quarantined = all(
                assessment.quarantined
                or _journal_candidate_uses_fact_evolution(
                    component_candidate,
                    runtime_config,
                )
                for component_candidate, assessment in zip(
                    component_candidates,
                    component_pollution,
                    strict=True,
                )
            )
            if all_fact_or_quarantined:
                component_results, source_checkpointed = (
                    _apply_structured_journal_fact_component_atomically(
                        conn,
                        scope=scope,
                        local_scope_id=local_scope_id,
                        shared_scope_id=shared_scope_id,
                        write_scope_ids=write_scope_ids,
                        run_id=run_id,
                        candidates=component_candidates,
                        pollution_assessments=component_pollution,
                        dry_run=dry_run,
                        runtime_config=runtime_config,
                    )
                )
                for counter_key, action in component_results:
                    counts[counter_key] += 1
                    if action.get("action") == "quarantine":
                        pollution_counts.update(action.get("reason_codes") or [])
                    actions.append(action)
                for component_candidate, assessment in zip(
                    component_candidates,
                    component_pollution,
                    strict=True,
                ):
                    ids = {
                        int(entry_id) for entry_id in component_candidate.entry_ids
                    }
                    if assessment.quarantined:
                        pollution_entry_ids.update(ids)
                    elif source_checkpointed:
                        processed_entry_ids.update(ids)
            else:
                mixed_result = _apply_mixed_journal_component_atomically(
                    conn,
                    vector_runtime,
                    scope,
                    run_id=run_id,
                    candidates=component_candidates,
                    dry_run=dry_run,
                    runtime_config=runtime_config,
                )
                counts.update(mixed_result["counts"])
                pollution_counts.update(mixed_result["pollution_counts"])
                actions.extend(mixed_result["actions"])
                processed_entry_ids.update(mixed_result["processed_entry_ids"])
                pollution_entry_ids.update(mixed_result.get("pollution_entry_ids") or [])
            continue
        if pollution.quarantined:
            counts["quarantined"] += 1
            pollution_counts.update(pollution.reason_codes)
            actions.append(
                {
                    "action": "quarantine",
                    "reason_codes": list(pollution.reason_codes),
                    "entry_ids": candidate.entry_ids,
                }
            )
            pollution_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                def _quarantine_unit() -> None:
                    _record_journal_rejection(
                        conn,
                        run_id=run_id,
                        entry_ids=candidate.entry_ids,
                        reason=(
                            "digest pollution: " + ",".join(pollution.reason_codes)
                        ),
                        candidate=candidate,
                    )

                _run_apply_unit(conn, candidate_index, _defer_commits, _quarantine_unit)
                if not _defer_commits:
                    conn.commit()
            continue
        if _journal_candidate_uses_fact_evolution(candidate, runtime_config):
            counter_key, action, source_checkpointed = (
                _apply_structured_journal_fact_atomically(
                    conn,
                    scope=scope,
                    local_scope_id=local_scope_id,
                    shared_scope_id=shared_scope_id,
                    write_scope_ids=write_scope_ids,
                    run_id=run_id,
                    candidate=candidate,
                    dry_run=dry_run,
                    runtime_config=runtime_config,
                )
            )
            counts[counter_key] += 1
            actions.append(action)
            if source_checkpointed:
                processed_entry_ids.update(
                    int(entry_id) for entry_id in candidate.entry_ids
                )
            continue
        if candidate.evolution is not None:
            candidate = replace(candidate, evolution=None)
        rejection_reason = _candidate_rejection_reason(candidate)
        if rejection_reason:
            # Rejected candidates still advance their source entries: the
            # durable outcome is the rejection receipt, not another retry of
            # the same low-quality text.
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": rejection_reason, "entry_ids": candidate.entry_ids})
            processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                def _skip_unit() -> None:
                    _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason=rejection_reason, candidate=candidate)

                _run_apply_unit(conn, candidate_index, _defer_commits, _skip_unit)
                if not _defer_commits:
                    conn.commit()
            continue
        candidate_scope_id = recall_scope_id_for_target(
            candidate.target,
            local_scope_id=local_scope_id,
            shared_scope_id=shared_scope_id,
            source="journal-digest",
        )
        match_id, _match_content, score = _find_match(
            conn,
            [candidate_scope_id],
            candidate,
        )
        match_scope_id = _memory_scope_id(conn, match_id) if match_id else ""
        match_is_writable = bool(match_scope_id == candidate_scope_id)
        if match_id and score >= 0.88:
            # High-confidence coverage is recorded as a rejection so operators
            # can audit why these journal rows were considered processed.
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": "existing memory covers candidate", "id": match_id, "score": round(score, 4), "entry_ids": candidate.entry_ids})
            processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                def _cover_unit() -> None:
                    _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason="existing memory covers candidate", candidate=candidate)

                _run_apply_unit(conn, candidate_index, _defer_commits, _cover_unit)
                if not _defer_commits:
                    conn.commit()
            continue
        if match_id and match_is_writable and score >= 0.55:
            # Similarity is a review signal, not authorization to rewrite an
            # existing durable row.  Store the automatic extraction as a
            # candidate so an audited merge/supersede decision can preserve
            # conflicting details and truthful timestamps.
            actions.append(
                {
                    "action": "merge_review",
                    "reason": "similar automatic candidate requires review",
                    "id": match_id,
                    "score": round(score, 4),
                    "entry_ids": candidate.entry_ids,
                }
            )
        memory_id = uuid.uuid4().hex
        counts["inserted"] += 1
        actions.append({"action": "insert", "id": memory_id, "target": candidate.target, "entry_ids": candidate.entry_ids})
        if not dry_run:
            def _store_unit() -> None:
                stored_id, summary, updated_at, inserted = store_row(
                    conn,
                    memory_id=memory_id,
                    scope_id=candidate_scope_id,
                    platform=scope.platform,
                    user_id=scope.user_id,
                    chat_id=scope.chat_id,
                    thread_id=scope.thread_id,
                    gateway_session_key=scope.gateway_session_key,
                    agent_identity=scope.agent_identity,
                    agent_workspace=scope.agent_workspace,
                    session_id=",".join(candidate.session_ids[:3]),
                    source="journal-digest",
                    target=candidate.target,
                    content=candidate.content,
                    metadata=json.dumps(
                        {
                            **_cross_platform_metadata(scope, runtime_config),
                            **candidate_metadata(
                                candidate,
                                run_id,
                                default_lifecycle=str(
                                    (runtime_config or {}).get(
                                        "automatic_digest_default_lifecycle"
                                    )
                                    or "candidate"
                                ),
                            ),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    commit=not _defer_commits,
                )
                if inserted:
                    _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                    processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
                    vector_event_id = _latest_current_vector_event_id(conn, stored_id)
                    payload = {
                        "event_id": vector_event_id,
                        "id": stored_id,
                        "source": "journal-digest",
                        "target": candidate.target,
                        "content": candidate.content,
                        "summary": summary,
                        "updated_at": updated_at,
                        "scope_id": candidate_scope_id,
                    }
                    if owns_external_drain:
                        owned_deferred.append(payload)
                    else:
                        vector_replay = _replay_or_defer_journal_vector(
                            vector_runtime,
                            _deferred_vector_ops,
                            payload,
                        )
                        if vector_event_id > 0 and _deferred_vector_ops is None:
                            actions[-1]["vector_replay"] = vector_replay
                    if vector_event_id > 0:
                        actions[-1]["vector_event_id"] = vector_event_id
                else:
                    counts["inserted"] -= 1
                    counts["updated"] += 1
                    actions.append({"action": "update", "reason": "duplicate store_row", "id": stored_id, "entry_ids": candidate.entry_ids})
                    _merge_metadata(conn, memory_id=stored_id, candidate=candidate, run_id=run_id)
                    _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                    processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)

            _run_apply_unit(conn, candidate_index, _defer_commits, _store_unit)
            if not _defer_commits:
                conn.commit()
    result = {
        "counts": dict(counts),
        "pollution_counts": dict(pollution_counts),
        "actions": actions,
        "processed_entry_ids": sorted(processed_entry_ids),
        "pollution_entry_ids": sorted(pollution_entry_ids),
    }
    if owns_external_drain:
        result["vector_replay"] = _commit_truth_then_drain_vector(
            conn,
            vector_runtime,
            owned_deferred,
            owns_transaction=not caller_owns_transaction,
        )
    return result




def _collect_journal_candidates(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    hermes_home: Path,
    scope: RuntimeScope,
    journal_config: dict[str, Any],
    requested_extractor: str,
) -> tuple[list[JournalDigestCandidate], str, str | dict[str, Any], Counter[str]]:
    """Collect candidates and keep structured extractor-failure metadata honest.

    When the LLM path returns sanitized failure metadata on the candidate list,
    that metadata is a ``dict``, not a string. Callers must not guess attempted
    IDs if the list lacks ``attempted_entry_ids``.
    """

    if requested_extractor == "llm":
        fallback_allowed = _config_bool(journal_config, "allow_heuristic_fallback", False)
        hard_error_kinds = {"endpoint_policy", "filtered", "parse"}
        try:
            candidates = llm_journal_candidates(conn, entries=entries, hermes_home=hermes_home, scope=scope, journal_config=journal_config)
            candidate_status_counts = Counter(getattr(candidates, "extractor_status_counts", {}) or {})
            extractor_error = getattr(candidates, "extractor_error", None)
            error_kind = ""
            if isinstance(extractor_error, dict):
                error_kind = str(
                    extractor_error.get("kind") or extractor_error.get("error_kind") or ""
                )
            if candidates:
                return candidates, "llm", "", candidate_status_counts
            if fallback_allowed and error_kind not in hard_error_kinds:
                reason = (
                    "llm failed; heuristic fallback enabled"
                    if extractor_error
                    else "llm produced no candidates"
                )
                return (
                    heuristic_journal_candidates(entries),
                    "heuristic-fallback",
                    reason,
                    candidate_status_counts,
                )
            if extractor_error:
                return candidates, "llm-error", extractor_error, candidate_status_counts
            return candidates, "llm", "", candidate_status_counts
        except Exception as exc:
            if isinstance(exc, active_journal_digest_llm_error()) and exc.error_kind in {
                "endpoint_policy",
                "filtered",
                "parse",
            }:
                raise
            if fallback_allowed:
                try:
                    return heuristic_journal_candidates(entries), "heuristic-fallback", "llm failed; heuristic fallback enabled", Counter()
                except Exception:
                    pass
            raise
    return heuristic_journal_candidates(entries), "heuristic", "", Counter()


def _scope_from_row(row: sqlite3.Row | None) -> RuntimeScope:
    return RuntimeScope(
        platform=str(row["platform"] if row else "telegram") or "telegram",
        user_id=str(row["user_id"] if row else "") or "local",
        chat_id=str(row["chat_id"] if row else ""),
        thread_id=str(row["thread_id"] if row else ""),
        gateway_session_key=str(row["gateway_session_key"] if row else ""),
        agent_identity=str(row["agent_identity"] if row else "default") or "default",
        agent_workspace=str(row["agent_workspace"] if row else "hermes") or "hermes",
        agent_context="primary",
    )


def _infer_scope_from_journal(conn: sqlite3.Connection) -> RuntimeScope:
    row = conn.execute(
        """
        SELECT platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace
        FROM journal_entries
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace
            FROM memories
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    return _scope_from_row(row)


def _unprocessed_scopes(
    conn: sqlite3.Connection,
    *,
    limit: int = 1000,
    excluded_chat_ids: frozenset[str] = frozenset(),
) -> list[tuple[RuntimeScope, str]]:
    """Return FIFO-owned unprocessed scopes paired with their stored scope_id.

    Grouping is by exact ``journal_entries.scope_id``. Readable local/shared/
    legacy aliases must not become a second claim on the same physical rows;
    each work unit owns only this stored id. ``accessible_scope_ids`` stays
    available for memory context, not for journal FIFO claiming.
    """

    clean_excluded = sorted(excluded_chat_ids)
    exclusion_sql = ""
    params: list[object] = []
    if clean_excluded:
        placeholders = ",".join("?" for _ in clean_excluded)
        exclusion_sql = f" AND COALESCE(chat_id, '') NOT IN ({placeholders})"
        params.extend(clean_excluded)
    rows = conn.execute(
        f"""
        SELECT platform, user_id, chat_id, thread_id, gateway_session_key,
               agent_identity, agent_workspace, scope_id, MIN(id) AS first_id
        FROM journal_entries
        WHERE (processed_run_id IS NULL OR processed_run_id = '')
          {exclusion_sql}
        GROUP BY scope_id
        ORDER BY first_id ASC
        LIMIT ?
        """,
        [*params, max(1, int(limit or 1000))],
    ).fetchall()
    return [(_scope_from_row(row), str(row["scope_id"] or "")) for row in rows]


def _open_digest_connection(db_path: Path, *, dry_run: bool) -> sqlite3.Connection:
    """Open the digest SQLite connection without installing fallible setup."""

    if dry_run:
        conn = connect_truth_database(":memory:", mode="rwc")
        try:
            if db_path.exists():
                source = connect_truth_database(db_path, mode="ro")
                try:
                    source.backup(conn)
                finally:
                    source.close()
            return conn
        except Exception:
            conn.close()
            raise
    return connect_truth_database(db_path, mode="rwc", timeout=30)


def _record_journal_digest_failure(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    started_at: str,
    extractor: str,
    interval_label: str,
    error: BaseException,
) -> None:
    """Record an error without erasing a committed partial receipt.

    If a running/partial receipt already exists, only status, error, and
    finished_at change. Committed counts, metadata, and leave evidence stay.
    A failure before the first truth commit still writes a zero-work error
    receipt. Never REPLACE unspecified columns away.
    """

    rollback_if_active(conn)
    ensure_journal_schema(conn)
    error_text = sanitize_report_text(str(error))[:1000]
    finished = now_iso()
    existing = conn.execute(
        "SELECT id FROM journal_digest_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE journal_digest_runs
            SET finished_at = ?, status = 'error', error = ?
            WHERE id = ?
            """,
            (finished, error_text, run_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO journal_digest_runs(
                id, started_at, finished_at, status, extractor, interval_label,
                processed_entries, inserted, updated, skipped, error, metadata
            ) VALUES (?, ?, ?, 'error', ?, ?, 0, 0, 0, 0, ?, '{}')
            """,
            (run_id, started_at, finished, extractor, interval_label, error_text),
        )
    conn.commit()


def _apply_loaded_leave_plan(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    leave: LeavePlan,
    unresolved_ids: set[int],
    retryable_unresolved_ids: set[int],
    quarantine_threshold: int,
    retryable_failures_threshold: int,
    dry_run: bool,
    counts: Counter[str],
    actions: list[dict[str, Any]],
) -> None:
    """Persist one loaded window's exclusive leave states and emit receipt actions.

    Deterministic and retryable-budget quarantine both flow through this plan so
    a row cannot be double-marked or double-counted.
    """

    actions.extend(
        leave_plan_receipt_actions(
            leave,
            unresolved_ids=unresolved_ids,
            retryable_unresolved_ids=retryable_unresolved_ids,
            quarantine_threshold=quarantine_threshold,
            retryable_failures_threshold=retryable_failures_threshold,
        )
    )
    if leave.deferred_ids:
        counts["deferred"] += len(leave.deferred_ids)
        if not dry_run:
            mark_entries_deferred(
                conn,
                entry_ids=leave.deferred_ids,
                run_id=run_id,
                commit=False,
            )
    if leave.attempts_quarantined_ids:
        counts["quarantined"] += len(leave.attempts_quarantined_ids)
        if not dry_run:
            _record_journal_rejection(
                conn,
                run_id=run_id,
                entry_ids=leave.attempts_quarantined_ids,
                reason="dead-letter:chunk extraction unresolved after bounded attempts",
                candidate=JournalDigestCandidate(
                    content=(
                        "Chunk extraction stayed unresolved after the "
                        "bounded attempt budget; quarantined for "
                        "journal-recovery replay."
                    ),
                    target="memory",
                    entry_ids=leave.attempts_quarantined_ids,
                ),
            )
    if leave.retryable_quarantined_ids:
        counts["quarantined"] += len(leave.retryable_quarantined_ids)
        counts["retryable_failures_quarantined"] += len(leave.retryable_quarantined_ids)
        if not dry_run:
            _record_journal_rejection(
                conn,
                run_id=run_id,
                entry_ids=leave.retryable_quarantined_ids,
                reason="retry-exhausted:persistent retryable LLM failure (timeout)",
                candidate=JournalDigestCandidate(
                    content=(
                        "Persistent retryable LLM failure exhausted the durable "
                        "cross-run budget; quarantined for journal-recovery replay."
                    ),
                    target="memory",
                    entry_ids=leave.retryable_quarantined_ids,
                ),
            )
    if leave.skipped_ids:
        counts["skipped"] += len(leave.skipped_ids)
        if not dry_run:
            _record_journal_rejection(
                conn,
                run_id=run_id,
                entry_ids=leave.skipped_ids,
                reason="no durable memory candidate",
                candidate=JournalDigestCandidate(
                    content="No durable memory candidate was produced for this reviewed journal entry.",
                    target="memory",
                    entry_ids=leave.skipped_ids,
                ),
            )


def _upsert_journal_digest_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    started_at: str,
    finished_at: str | None,
    status: str,
    extractor: str,
    interval_label: str,
    processed_entries: int,
    inserted: int,
    updated: int,
    skipped: int,
    error: str | None,
    metadata: str,
) -> None:
    """Insert or update the digest receipt that describes committed truth.

    Per-scope commits must upsert a sanitized running/partial receipt in the
    same SQLite transaction as cursor/leave writes. Final completion updates
    the same ``journal_digest_runs.id``.
    """

    conn.execute(
        """
        INSERT INTO journal_digest_runs(
            id, started_at, finished_at, status, extractor, interval_label,
            processed_entries, inserted, updated, skipped, error, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            finished_at = excluded.finished_at,
            status = excluded.status,
            extractor = excluded.extractor,
            interval_label = excluded.interval_label,
            processed_entries = excluded.processed_entries,
            inserted = excluded.inserted,
            updated = excluded.updated,
            skipped = excluded.skipped,
            error = excluded.error,
            metadata = excluded.metadata
        """,
        (
            run_id,
            started_at,
            finished_at,
            status,
            extractor,
            interval_label,
            processed_entries,
            inserted,
            updated,
            skipped,
            error,
            metadata,
        ),
    )


def _dynamic_journal_digest_limit(
    conn: sqlite3.Connection,
    *,
    configured_limit: int,
    journal_config: dict[str, Any],
    excluded_chat_ids: frozenset[str] = frozenset(),
) -> int:
    if not _config_bool(journal_config, "dynamic_max_entries_enabled", True):
        return configured_limit
    backlog = _journal_unprocessed_count(
        conn,
        excluded_chat_ids=excluded_chat_ids,
    )
    auto_threshold = max(1, configured_limit * 4)
    # Merged code defaults used 2000/1200 for a 500-entry window. Live
    # Tianshu overrode the window to 80 but kept those defaults, so a
    # 999-entry backlog never scaled. Never let the trigger sit above
    # 4x the current window.
    threshold = min(
        _coerce_positive_int(
            journal_config.get("dynamic_backlog_threshold"), auto_threshold
        ),
        auto_threshold,
    )
    if backlog <= threshold:
        return configured_limit
    default_ceiling = max(configured_limit, 500)
    auto_ceiling = max(default_ceiling, configured_limit * 8)
    ceiling = min(
        _coerce_positive_int(
            journal_config.get("max_entries_per_digest_ceiling"), default_ceiling
        ),
        auto_ceiling,
    )
    return min(backlog, max(configured_limit, ceiling))



def run_journal_digest(
    *,
    hermes_home: Path,
    extractor: str = "llm",
    scope: RuntimeScope | None = None,
    interval_label: str = "manual",
    limit_entries: int | None = None,
    dry_run: bool = False,
    llm_provider: str = "",
    llm_model: str = "",
    llm_api_mode: str = "",
    llm_base_url: str = "",
    llm_endpoint: str = "",
    llm_key_env: str = "",
    llm_api_key: str = "",
    llm_append_v1: bool | None = None,
    llm_allow_insecure_endpoint: bool | None = None,
) -> dict[str, Any]:
    """Run one bounded journal digest pass and record visible status metadata.

    Digest extraction may use heuristic or LLM paths, but every failure mode should become a structured status, dead-letter, or quarantine signal instead of disappearing as empty output."""
    hermes_home = hermes_home.expanduser().resolve()
    storage_dir = hermes_home / "scope-recall"
    writer_lease: TruthWriterLease | None = None
    conn: sqlite3.Connection | None = None
    vector_runtime = None
    prev_vector = None
    if not dry_run:
        storage_dir.mkdir(parents=True, exist_ok=True)
        writer_lease = TruthWriterLease(storage_dir, role="journal_digest")
        lease_result = writer_lease.acquire()
        if lease_result.get("status") != "acquired":
            owner = lease_result.get("owner")
            raise TruthWriterBusyError(
                role="journal_digest",
                scope=str(lease_result.get("scope") or ""),
                owner=owner if isinstance(owner, dict) else {},
            )
    db_path = storage_dir / "memory.sqlite3"
    run_id = uuid.uuid4().hex
    started_at = now_iso()
    try:
        conn = _open_digest_connection(db_path, dry_run=dry_run)
        if not dry_run:
            install_activation_lease_authorizer(conn, db_path)
        conn.row_factory = sqlite3.Row
        runtime_config = _runtime_config(hermes_home)
        excluded_chat_ids = memory_isolated_chat_ids(runtime_config)
        raw_journal = runtime_config.get("journal")
        journal_config = dict(raw_journal) if isinstance(raw_journal, dict) else {}
        llm_overrides = {
            "provider": llm_provider,
            "model": llm_model,
            "api_mode": llm_api_mode,
            "base_url": llm_base_url,
            "endpoint": llm_endpoint,
            "key_env": llm_key_env,
            "api_key": llm_api_key,
        }
        for key, value in llm_overrides.items():
            if str(value or "").strip():
                journal_config[key] = str(value).strip()
        if llm_append_v1 is not None:
            journal_config["append_v1"] = bool(llm_append_v1)
        if llm_allow_insecure_endpoint is not None:
            journal_config["allow_insecure_endpoint"] = (
                explicit_insecure_endpoint_opt_in(llm_allow_insecure_endpoint)
            )
        configured_limit = _coerce_positive_int(journal_config.get("max_entries_per_digest"), 500)
        effective_limit = _coerce_positive_int(limit_entries, configured_limit) if limit_entries is not None else configured_limit
        retention_days = int(journal_config.get("retention_days") or 0)
        requested_extractor = str(extractor or journal_config.get("extractor") or "llm").strip().lower()
        extractor_used = requested_extractor
        ensure_schema(conn)
        ensure_journal_schema(conn)
        if scope is not None and scope_is_memory_isolated(scope, runtime_config):
            result = no_unprocessed_journal_result(
                run_id=run_id,
                requested_extractor=requested_extractor,
                extractor_used=extractor_used,
            )
            result["status"] = "source_isolated"
            result["source_isolated"] = True
            return result
        if limit_entries is None:
            effective_limit = _dynamic_journal_digest_limit(
                conn,
                configured_limit=configured_limit,
                journal_config=journal_config,
                excluded_chat_ids=excluded_chat_ids,
            )
        backlog_before = _journal_unprocessed_count(
            conn,
            excluded_chat_ids=excluded_chat_ids,
        )
        work_units = (
            [(scope, "")]
            if scope is not None
            else _unprocessed_scopes(
                conn,
                limit=effective_limit,
                excluded_chat_ids=excluded_chat_ids,
            )
        )
        if not work_units:
            return no_unprocessed_journal_result(run_id=run_id, requested_extractor=requested_extractor, extractor_used=extractor_used)

        total_loaded_entries = 0
        total_candidates = 0
        processed_entry_ids: list[int] = []
        counts = Counter()
        extractor_counts = Counter()
        quarantine_counts = Counter()
        candidate_status_counts = Counter()
        extractor_errors: list[Any] = []
        extraction_failure_count = 0
        actions: list[dict[str, Any]] = []
        leave_processed_ids: set[int] = set()
        leave_pending_ids: set[int] = set()
        leave_deferred_ids: set[int] = set()
        leave_quarantined_ids: set[int] = set()
        prev_scope_pending = False
        prev_deferred: list[dict[str, Any]] = []
        prev_vector = None
        claimed_entry_ids: set[int] = set()

        def persist_digest_receipt(
            *,
            status: str,
            receipt_kind: str,
            error: str | None,
            pruned: int = 0,
        ) -> None:
            """Upsert the digest receipt that must commit with current truth."""

            current_extractor = requested_extractor
            if extractor_counts:
                current_extractor = (
                    next(iter(extractor_counts))
                    if len(extractor_counts) == 1
                    else "mixed"
                )
            current_processed = sorted(set(processed_entry_ids))
            leave_states = {
                "processed_ids": sorted(leave_processed_ids),
                "retryable_pending_ids": sorted(leave_pending_ids),
                "deferred_ids": sorted(leave_deferred_ids),
                "quarantined_ids": sorted(leave_quarantined_ids),
            }
            backlog_now = backlog_before
            try:
                backlog_now = _journal_unprocessed_count(
                    conn,
                    excluded_chat_ids=excluded_chat_ids,
                )
            except Exception:
                backlog_now = backlog_before
            recommended_now = _dynamic_journal_digest_limit(
                conn,
                configured_limit=configured_limit,
                journal_config=journal_config,
                excluded_chat_ids=excluded_chat_ids,
            )
            receipt_now = journal_digest_receipt_fields(
                total_loaded_entries=total_loaded_entries,
                total_candidates=total_candidates,
                counts=counts,
                quarantine_counts=quarantine_counts,
                extractor_errors=extractor_errors,
                backlog_before=backlog_before,
                backlog_after=backlog_now,
                effective_limit=effective_limit,
                recommended_next_limit=recommended_now,
                candidate_status_counts=candidate_status_counts,
            )
            _upsert_journal_digest_run(
                conn,
                run_id=run_id,
                started_at=started_at,
                finished_at=now_iso(),
                status=status,
                extractor=current_extractor,
                interval_label=interval_label,
                processed_entries=len(current_processed),
                inserted=int(counts.get("inserted", 0) or 0),
                updated=int(counts.get("updated", 0) or 0),
                skipped=int(counts.get("skipped", 0) or 0),
                error=error,
                metadata=json.dumps(
                    journal_digest_metadata(
                        total_candidates=total_candidates,
                        total_loaded_entries=total_loaded_entries,
                        actions=actions,
                        requested_extractor=requested_extractor,
                        extractor_used=current_extractor,
                        extractor_counts=extractor_counts,
                        extractor_errors=extractor_errors,
                        quarantine_counts=quarantine_counts,
                        backlog_before=backlog_before,
                        effective_limit=effective_limit,
                        retention_days=retention_days,
                        pruned_entries=pruned,
                        backlog_after=receipt_now["backlog_after"],
                        productive_writes=receipt_now["productive_writes"],
                        no_insert_reason=receipt_now["no_insert_reason"],
                        health_flags=receipt_now["health_flags"],
                        recommended_next_limit=receipt_now["recommended_next_limit"],
                        candidate_status_counts=candidate_status_counts,
                        retryable_failures=int(counts.get("retryable_failures", 0) or 0),
                        retryable_failures_quarantined=int(
                            counts.get("retryable_failures_quarantined", 0) or 0
                        ),
                        leave_states=leave_states,
                        receipt_kind=receipt_kind,
                    ),
                    ensure_ascii=False,
                ),
            )

        for active_scope, owner_scope_id in work_units:
            scope_deferred: list[dict[str, Any]] = []
            remaining = max(0, effective_limit - total_loaded_entries)
            if remaining <= 0:
                break
            if prev_scope_pending and not dry_run:
                persist_digest_receipt(
                    status="running",
                    receipt_kind="partial",
                    error=None,
                )
                _commit_truth_then_drain_vector(conn, prev_vector, prev_deferred)
                if prev_vector is not None:
                    prev_vector.close()
                prev_scope_pending = False
                prev_deferred = []
                prev_vector = None
            active_scope = normalize_scope_identity(active_scope, runtime_config)
            readable_scope_ids = accessible_scope_ids(active_scope, runtime_config)
            # Explicit ``scope=`` keeps readable aliases for that one identity.
            # Background work units claim only the stored owner scope_id so
            # local/shared/legacy overlap cannot reload the same physical rows.
            claim_scope_ids = (
                [owner_scope_id] if owner_scope_id else readable_scope_ids
            )
            entries = load_unprocessed_journal_entries(
                conn,
                scope_ids=claim_scope_ids,
                limit=remaining,
                excluded_chat_ids=excluded_chat_ids,
                per_session_limit=journal_config.get("max_entries_per_session_per_run") or 0,  # type: ignore[arg-type]
            )
            entries = [
                entry for entry in entries if int(entry.id) not in claimed_entry_ids
            ]
            if not entries:
                continue
            claimed_entry_ids.update(int(entry.id) for entry in entries)
            digestible, evidence_only = split_digestible_entries(entries)
            total_loaded_entries += len(entries)
            if not conn.in_transaction:
                release_snapshot_transaction(conn)
                prepare_network_boundary(conn, "journal.run_journal_digest.snapshot")
            raised_without_attempted = False
            try:
                collected: Any = _collect_journal_candidates(
                    conn,
                    entries=digestible,
                    hermes_home=hermes_home,
                    scope=active_scope,
                    journal_config=journal_config,
                    requested_extractor=requested_extractor,
                )
                if len(collected) == 3:
                    candidates, scope_extractor_used, extractor_error = collected
                    scope_candidate_status_counts = Counter()
                else:
                    candidates, scope_extractor_used, extractor_error, scope_candidate_status_counts = collected
                if candidates is None:
                    candidates = []
                candidates = attach_digestible_tool_provenance(candidates, digestible)
            except Exception as exc:
                if requested_extractor != "llm":
                    raise
                # No attempted-ID metadata: fail closed. Do not guess the
                # loaded/digestible window, and charge no retryable or
                # deterministic budget.
                raised_without_attempted = True
                scope_extractor_used = "llm-error"
                _failure_reason, failure_meta = _quarantine_classification(exc)
                extractor_error = failure_meta
                scope_candidate_status_counts = Counter()
                candidates = []
                extraction_failure_count += 1
                pending_entry_ids = [int(entry.id) for entry in digestible]
                actions.append(
                    {
                        "action": "error",
                        "reason": "extractor failure; source entries remain pending",
                        "entry_count": len(pending_entry_ids),
                        "entry_ids": pending_entry_ids[:20],
                        "classification": failure_meta,
                    }
                )
            if not dry_run and not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            extractor_counts[scope_extractor_used] += 1
            candidate_status_counts.update(scope_candidate_status_counts)
            if extractor_error:
                extractor_errors.append(extractor_error)
            if scope_extractor_used == "llm-error" and not raised_without_attempted:
                extraction_failure_count += 1
            total_candidates += len(candidates)
            candidate_entry_ids: set[int] = set()
            for candidate in candidates:
                for entry_id in candidate.entry_ids:
                    try:
                        candidate_entry_ids.add(int(entry_id))
                    except (TypeError, ValueError):
                        continue
            loaded_entry_ids = {int(entry.id) for entry in entries}
            evidence_ids = {int(entry.id) for entry in evidence_only}
            admission_ids: list[int] = []
            if evidence_only:
                for entry in evidence_only:
                    entry_id = int(entry.id)
                    admission_ids.append(entry_id)
                    counts["skipped"] += 1
                    if not dry_run:
                        _record_journal_rejection(
                            conn,
                            run_id=run_id,
                            entry_ids=[entry_id],
                            reason=admission_leave_reason(entry),
                            candidate=JournalDigestCandidate(
                                content=str(entry.content or ""),
                                target="memory",
                                entry_ids=[entry_id],
                            ),
                        )
            if hasattr(candidates, "reviewed_entry_ids"):
                reviewed_entry_ids = {
                    int(entry_id)
                    for entry_id in getattr(candidates, "reviewed_entry_ids", set())
                } & loaded_entry_ids
                reviewed_entry_ids -= evidence_ids
                unresolved_entry_ids = {
                    int(entry_id)
                    for entry_id in getattr(candidates, "unresolved_entry_ids", set())
                } & loaded_entry_ids
                unresolved_entry_ids -= evidence_ids
            elif raised_without_attempted:
                reviewed_entry_ids = set()
                unresolved_entry_ids = set()
            else:
                reviewed_entry_ids = set(loaded_entry_ids) - evidence_ids
                unresolved_entry_ids = set()
            if not dry_run:
                try:
                    from .nightly_digest import DigestVectorRuntime, ScopeProfile

                    vector_runtime = DigestVectorRuntime(
                        hermes_home=hermes_home,
                        conn=conn,
                        scope=ScopeProfile(
                            scope=active_scope,
                            scope_id=build_scope_id(active_scope, runtime_config),
                            shared_scope_id=build_shared_scope_id(active_scope, runtime_config),
                            accessible_scope_ids=accessible_scope_ids(active_scope, runtime_config),
                        ),
                    )
                except Exception:
                    vector_runtime = None
            applied = apply_journal_candidates(
                conn,
                vector_runtime,
                active_scope,
                run_id=run_id,
                candidates=candidates,
                dry_run=dry_run,
                runtime_config=runtime_config,
                _defer_commits=True,
                _deferred_vector_ops=scope_deferred if not dry_run else None,
            )
            counts.update(applied["counts"])
            quarantine_counts.update(applied.get("pollution_counts", {}))
            applied_entry_ids = {int(entry_id) for entry_id in applied.get("processed_entry_ids", [])}
            pollution_entry_ids = {
                int(entry_id) for entry_id in applied.get("pollution_entry_ids", [])
            }
            retryable_unresolved_ids = {
                int(entry_id)
                for entry_id in getattr(
                    candidates, "retryable_unresolved_entry_ids", set()
                )
            } & loaded_entry_ids
            has_attempted_meta = hasattr(candidates, "attempted_entry_ids")
            attempted_entry_ids = {
                int(entry_id)
                for entry_id in getattr(candidates, "attempted_entry_ids", set())
            } & loaded_entry_ids
            if has_attempted_meta:
                retryable_unresolved_ids &= attempted_entry_ids
            extractor_deferred_ids = {
                int(entry_id)
                for entry_id in getattr(candidates, "deferred_entry_ids", set())
            } & loaded_entry_ids
            # Only IDs proven to have reached `_call_llm_with_retries` may
            # increment or reset the durable retryable budget. Unattempted
            # suffixes, evidence-only rows, and exception paths without
            # attempted metadata charge nothing.
            retryable_failures_after: dict[int, int] = {}
            if has_attempted_meta and attempted_entry_ids and not dry_run:
                increment_ids = sorted(
                    attempted_entry_ids & retryable_unresolved_ids
                )
                reset_ids = sorted(attempted_entry_ids - retryable_unresolved_ids)
                if increment_ids:
                    retryable_failures_after = increment_retryable_failures(
                        conn, entry_ids=increment_ids, commit=False
                    )
                    counts["retryable_failures"] += len(increment_ids)
                if reset_ids:
                    reset_retryable_failures(
                        conn, entry_ids=reset_ids, commit=False
                    )
            quarantine_threshold = _coerce_positive_int(
                journal_config.get("extraction_attempts_quarantine"), 3
            )
            retryable_failures_threshold = _coerce_positive_int(
                journal_config.get("retryable_failures_quarantine"), 3
            )
            if has_attempted_meta:
                countable_ids = sorted(
                    (
                        (unresolved_entry_ids - candidate_entry_ids)
                        - retryable_unresolved_ids
                    )
                    & attempted_entry_ids
                )
            elif raised_without_attempted:
                countable_ids = []
            else:
                countable_ids = sorted(
                    (unresolved_entry_ids - candidate_entry_ids)
                    - retryable_unresolved_ids
                )
            attempts_after: dict[int, int] = {}
            if countable_ids and not dry_run:
                attempts_after = increment_extraction_attempts(
                    conn, entry_ids=countable_ids, commit=False
                )
            leave = plan_loaded_leave(
                loaded_ids=loaded_entry_ids - evidence_ids,
                candidate_ids=candidate_entry_ids,
                reviewed_ids=reviewed_entry_ids,
                unresolved_ids=unresolved_entry_ids,
                retryable_unresolved_ids=retryable_unresolved_ids,
                deferred_ids=extractor_deferred_ids,
                applied_ids=applied_entry_ids,
                pollution_ids=pollution_entry_ids,
                attempts_after=attempts_after,
                quarantine_threshold=quarantine_threshold,
                retryable_failures_after=retryable_failures_after,
                retryable_failures_threshold=retryable_failures_threshold,
            )
            if not dry_run:
                clear_current_deferral(
                    conn,
                    entry_ids=sorted(loaded_entry_ids - set(leave.deferred_ids)),
                    commit=False,
                )
            _apply_loaded_leave_plan(
                conn,
                run_id=run_id,
                leave=leave,
                unresolved_ids=unresolved_entry_ids,
                retryable_unresolved_ids=retryable_unresolved_ids,
                quarantine_threshold=quarantine_threshold,
                retryable_failures_threshold=retryable_failures_threshold,
                dry_run=dry_run,
                counts=counts,
                actions=actions,
            )
            leave_sets = loaded_leave_sets(leave, admission_ids=admission_ids)
            leave_processed_ids.update(leave_sets["processed_ids"])
            leave_pending_ids.update(leave_sets["retryable_pending_ids"])
            leave_deferred_ids.update(leave_sets["deferred_ids"])
            leave_quarantined_ids.update(leave_sets["quarantined_ids"])
            scope_done_ids = sorted(
                set(leave.applied_ids)
                | set(leave.skipped_ids)
                | set(leave.quarantined_ids)
                | set(admission_ids)
            )
            processed_entry_ids.extend(scope_done_ids)
            if not dry_run and scope_done_ids:
                mark_entries_processed(
                    conn, entry_ids=scope_done_ids, run_id=run_id, commit=False
                )
            if not dry_run:
                advance_session_digest_cursors(
                    conn,
                    entries=entries,
                    covered_ids=loaded_entry_ids - set(leave.deferred_ids),
                    deferred_ids=set(leave.deferred_ids),
                    run_id=run_id,
                    commit=False,
                )
            actions.extend(applied["actions"])
            prev_scope_pending = True
            prev_deferred = scope_deferred
            prev_vector = vector_runtime
            vector_runtime = None

        if total_loaded_entries == 0:
            return no_unprocessed_journal_result(run_id=run_id, requested_extractor=requested_extractor, extractor_used=extractor_used)
        unique_processed_entry_ids = sorted(set(processed_entry_ids))
        if extractor_counts:
            extractor_used = next(iter(extractor_counts)) if len(extractor_counts) == 1 else "mixed"
        else:
            extractor_used = requested_extractor
        pruned_entries = 0
        backlog_after = backlog_before
        if not dry_run:
            mark_entries_processed(conn, entry_ids=unique_processed_entry_ids, run_id=run_id, commit=False)
            pruned_entries = _prune_processed_journal(conn, retention_days=retention_days, commit=False)
            backlog_after = _journal_unprocessed_count(
                conn,
                excluded_chat_ids=excluded_chat_ids,
            )
        recommended_next_limit = _dynamic_journal_digest_limit(
            conn,
            configured_limit=configured_limit,
            journal_config=journal_config,
            excluded_chat_ids=excluded_chat_ids,
        )
        receipt_fields = journal_digest_receipt_fields(
            total_loaded_entries=total_loaded_entries,
            total_candidates=total_candidates,
            counts=counts,
            quarantine_counts=quarantine_counts,
            extractor_errors=extractor_errors,
            backlog_before=backlog_before,
            backlog_after=backlog_after,
            effective_limit=effective_limit,
            recommended_next_limit=recommended_next_limit,
            candidate_status_counts=candidate_status_counts,
        )
        run_status = "error" if extraction_failure_count else "ok"
        run_error = ""
        if extraction_failure_count:
            error_kinds = sorted(
                {
                    str(item.get("kind") or "unknown")
                    for item in extractor_errors
                    if isinstance(item, dict)
                }
            )
            run_error = sanitize_report_text(
                "LLM extraction failed "
                f"({', '.join(error_kinds) or 'unknown'}); source entries remain pending"
            )
        if not dry_run:
            persist_digest_receipt(
                status=run_status,
                receipt_kind="final",
                error=run_error or None,
                pruned=pruned_entries,
            )
            _commit_truth_then_drain_vector(conn, prev_vector, prev_deferred)
            if prev_vector is not None:
                prev_vector.close()
                prev_vector = None
            prev_scope_pending = False
        result = journal_digest_success_result(
            dry_run=dry_run,
            run_id=run_id,
            total_loaded_entries=total_loaded_entries,
            processed_entry_count=len(unique_processed_entry_ids),
            total_candidates=total_candidates,
            counts=counts,
            requested_extractor=requested_extractor,
            extractor_used=extractor_used,
            quarantine_counts=quarantine_counts,
            backlog_before=backlog_before,
            effective_limit=effective_limit,
            pruned_entries=pruned_entries,
            actions=actions,
            backlog_after=receipt_fields["backlog_after"],
            productive_writes=receipt_fields["productive_writes"],
            no_insert_reason=receipt_fields["no_insert_reason"],
            health_flags=receipt_fields["health_flags"],
            recommended_next_limit=receipt_fields["recommended_next_limit"],
            candidate_status_counts=candidate_status_counts,
            retryable_failures=int(counts.get("retryable_failures", 0) or 0),
            retryable_failures_quarantined=int(
                counts.get("retryable_failures_quarantined", 0) or 0
            ),
        )
        result["leave_states"] = {
            "processed_ids": sorted(leave_processed_ids),
            "retryable_pending_ids": sorted(leave_pending_ids),
            "deferred_ids": sorted(leave_deferred_ids),
            "quarantined_ids": sorted(leave_quarantined_ids),
        }
        if extraction_failure_count:
            result["ok"] = False
            result["status"] = "error"
            result["error"] = run_error
        return result
    except Exception as exc:
        try:
            if dry_run:
                rollback_if_active(conn)
            elif conn is not None:
                _record_journal_digest_failure(
                    conn,
                    run_id=run_id,
                    started_at=started_at,
                    extractor=requested_extractor,
                    interval_label=interval_label,
                    error=exc,
                )
        except Exception as receipt_exc:
            # Preserve the triggering exception even when SQLite is still too
            # contended to persist its failure receipt.
            try:
                rollback_if_active(conn)
            except Exception:
                pass
            logger.warning(
                "Scope Recall journal digest failure receipt could not be persisted (%s)",
                type(receipt_exc).__name__,
            )
        raise
    finally:
        vector_close_error: Exception | None = None
        for pending in (vector_runtime, prev_vector):
            if pending is None:
                continue
            try:
                pending.close()
            except Exception as exc:
                if vector_close_error is None:
                    vector_close_error = exc
        if conn is not None:
            conn.close()
        if writer_lease is not None:
            writer_lease.release()
        if vector_close_error is not None:
            raise vector_close_error


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Digest scope-recall journal entries into high-quality durable memories")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--extractor", choices=["llm", "heuristic"], default="llm", help="Extraction backend; default is LLM-first. Use heuristic only as an explicit operator fallback.")
    parser.add_argument("--interval-label", default="manual", help="Human-readable schedule label, e.g. 2h")
    parser.add_argument("--limit-entries", type=int, default=None, help="Maximum unprocessed journal entries per run; defaults to journal.max_entries_per_digest")
    parser.add_argument("--provider", default="", help="LLM provider name from Hermes config, e.g. deepseek; overrides main model provider for this digest run")
    parser.add_argument("--model", default="", help="LLM model for this digest run")
    parser.add_argument("--api-mode", default="", choices=["", "chat_completions", "codex_responses", "anthropic_messages"], help="LLM API mode for this digest run")
    parser.add_argument("--base-url", default="", help="LLM base URL for this digest run")
    parser.add_argument("--endpoint", default="", help="Full chat-completions endpoint override for this digest run")
    parser.add_argument("--key-env", default="", help="Environment variable name containing the LLM API key")
    parser.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    append_group = parser.add_mutually_exclusive_group()
    append_group.add_argument("--append-v1", dest="append_v1", action="store_true", default=None, help="Append /v1 before /chat/completions when using base-url")
    append_group.add_argument("--no-append-v1", dest="append_v1", action="store_false", help="Do not append /v1 before /chat/completions when using base-url")
    parser.add_argument("--allow-insecure-endpoint", action=argparse.BooleanOptionalAction, default=None, help="Allow an explicitly trusted non-loopback HTTP endpoint; credential headers are still stripped")
    parser.add_argument("--dry-run", action="store_true", help="Plan without writing memories or advancing watermarks")
    parser.add_argument("--verbose", action="store_true", help="Print full JSON result")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    started = time.time()
    try:
        result = run_journal_digest(
            hermes_home=Path(args.hermes_home),
            extractor=str(args.extractor),
            interval_label=str(args.interval_label),
            limit_entries=max(1, int(args.limit_entries)) if args.limit_entries is not None else None,
            dry_run=bool(args.dry_run),
            llm_provider=str(args.provider or ""),
            llm_model=str(args.model or ""),
            llm_api_mode=str(args.api_mode or ""),
            llm_base_url=str(args.base_url or ""),
            llm_endpoint=str(args.endpoint or ""),
            llm_key_env=str(args.key_env or ""),
            llm_api_key=str(args.api_key or ""),
            llm_append_v1=args.append_v1,
            llm_allow_insecure_endpoint=args.allow_insecure_endpoint,
        )
        result["elapsed_seconds"] = round(time.time() - started, 3)
        if args.verbose:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            compact = {
                key: result.get(key)
                for key in (
                    "ok",
                    "status",
                    "processed_entries",
                    "candidates",
                    "inserted",
                    "updated",
                    "skipped",
                    "productive_writes",
                    "backlog_before",
                    "backlog_after",
                    "backlog_delta",
                    "quarantine_counts",
                    "health_flags",
                )
            }
            print(json.dumps(compact, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": sanitize_report_text(str(exc))}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
