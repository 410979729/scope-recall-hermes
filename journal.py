"""Compatibility facade and orchestration layer for journal capture, digest, and recovery helpers.

Many public imports historically pointed here, so split-out modules are re-exported while preserving old monkeypatch surfaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .capture_filters import sanitize_report_text, sanitize_structured_value, should_capture_text
from .digest_pollution import assess_digest_batch
from .digest_run_results import journal_digest_metadata, journal_digest_receipt_fields, journal_digest_success_result, no_unprocessed_journal_result
from .fact_actions import EvolutionAction
from .fact_evolution import (
    execute_pipeline_proposal,
    fact_evolution_enabled,
    memory_type_uses_fact_evolution,
)
from .gating import clean_text, compact_text, dedup_key
from .governance import semantic_similarity
from .http_utils import explicit_insecure_endpoint_opt_in
from .maintenance_lease import install_activation_lease_authorizer
from .models import RuntimeScope, recall_scope_id_for_target
from .transaction_guard import prepare_network_boundary
from .truth_connection import connect_truth_database
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
    ensure_journal_schema,
    load_unprocessed_journal_entries,
    mark_entries_processed,
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

JOURNAL_TARGETS = {"user", "memory", "project", "ops", "general"}



LOW_VALUE_NOTIFICATION_RE = re.compile(
    r"\b(?:webhook|web\s+hook|bot\s+(?:push|message|status)|notification|push\s+message|"
    r"sign[-\s]?in|check[-\s]?in|subscription|subscribed|unsubscribe|qas)\b|"
    r"(?:通知|推送|机器人消息|签到|签入|登录提醒|订阅(?:更新|通知)?)",
    re.IGNORECASE,
)
LOW_VALUE_LOG_RE = re.compile(
    r"\b(?:docker\s+logs?|journalctl|kubectl\s+logs?|stack\s+trace|traceback|stderr|stdout|"
    r"shell\s+(?:prompt|output)|terminal\s+output|command\s+output|tool\s+(?:execution\s+)?summary|tool\s+result)\b|"
    r"(?:工具执行摘要|工具结果|命令输出|终端输出|日志输出|堆栈|调用栈)",
    re.IGNORECASE,
)
LOW_VALUE_PROGRESS_RE = re.compile(
    r"\b(?:backup\s+path|temporary\s+file|run\s+result|task\s+progress|no\s+action\s+required|"
    r"one[-\s]?off|status\s+update)\b|(?:临时文件|备份路径|任务进度|一次性|无需处理|状态更新)",
    re.IGNORECASE,
)
EPHEMERAL_RELEASE_STATE_RE = re.compile(
    r"(?:session\s+[`'\"]?\d{8}_[0-9a-f_]+|\bHEAD\s*=|\borigin/main\b|\bgit\s+status\b|"
    r"\b(?:pushed|local|closed|open)\s+(?:commits?|issues?)\b|\bissue\s*#?\d+\b|#\d+\s+`|"
    r"\b(?:commit|tag|branch)\s+[`'\"]?[0-9a-f]{7,40}\b|\b[0-9a-f]{7,40}\b.*\b(?:commit|HEAD|origin/main)\b|"
    r"\b\d+\s+passed\b|\bpyright\b.*\b(?:warning|error)s?\b|\bruff\s+(?:pass|passed|全通过)\b|"
    r"(?:未\s*commit|未\s*push|未\s*tag|不\s*tag|不\s*release|已关闭\s*issue|记录时状态|发布候选|当前进度))",
    re.IGNORECASE,
)
TRANSIENT_PHASE_GATE_RE = re.compile(
    r"(?:当前阶段|这个阶段|现阶段|下一步|继续下一步|不要急着|先(?:进行)?阶段性?验证|先验证|再进(?:入)?\s*[A-Z]\d|进入\s*[A-Z]\d|"
    r"阶段性验收|全量\s*pytest|live\s+doctor|rollout\s+profiles\s+dry-run|可选复审|"
    r"current\s+phase|next\s+step|phase[-\s]?gate|before\s+entering\s+[A-Z]\d|run\s+full\s+pytest|live\s+doctor)",
    re.IGNORECASE,
)
HIGH_VALUE_DURABLE_SIGNAL_RE = re.compile(
    r"\b(?:preference|prefers|constraint|policy|api\s+boundary|environment\s+fact|root\s+cause|"
    r"fix|workaround|verification|verified|reusable|workflow|procedure|runbook|pitfall|"
    r"design\s+decision|stable|must|should|requires?|rollback|guardrail)\b|"
    r"(?:偏好|约束|边界|环境事实|根因|修复|验证|可复用|流程|步骤|规程|坑|设计决策|稳定|必须|应该|回滚|防护)",
    re.IGNORECASE,
)


def _has_high_value_durable_signal(text: str) -> bool:
    return bool(HIGH_VALUE_DURABLE_SIGNAL_RE.search(text or ""))


def _low_value_promotion_reason(candidate: JournalDigestCandidate) -> str:
    """Return a rejection reason for obvious journal-digest promotion noise.

    Capture filters protect raw journal ingestion, but an LLM digest can rephrase
    webhook/log/tool noise into a plausible durable fact.  This second gate is
    intentionally conservative: only obvious notification/log/progress shapes are
    blocked, and root-cause/fix/workflow/preference/constraint signals still pass.
    """
    text = clean_text(candidate.content)
    if not text:
        return "low-value-empty"
    if "[REDACTED_PATH]" in text or "Artifact anchors:" in text or "artifact anchors:" in text:
        return "low-value-redacted-path-or-artifact-anchor"
    if candidate.target == "project" and re.search(r"(?:当前系统现状|当前技术现状|当前系统状态|当前状态|技术债务|current status|current state|technical debt)", text, re.IGNORECASE):
        return "low-value-stale-status-snapshot"
    has_value_signal = _has_high_value_durable_signal(text)
    if candidate.memory_type == "tool_trace" and not has_value_signal:
        return "low-value-tool-trace"
    tag_set = {str(tag).strip().lower() for tag in candidate.tags or []}
    if TRANSIENT_PHASE_GATE_RE.search(text) and (
        candidate.memory_type in {"decision", "summary", "workflow"}
        or candidate.target == "project"
        or tag_set & {"phase-gate", "project-management", "status", "progress"}
    ):
        return "low-value-transient-phase-gate"
    stable_release_knowledge = has_value_signal and candidate.memory_type in {"constraint", "pitfall", "procedure", "workflow"} and candidate.target != "project"
    if EPHEMERAL_RELEASE_STATE_RE.search(text) and not stable_release_knowledge:
        return "low-value-ephemeral-release-or-issue-state"
    if LOW_VALUE_NOTIFICATION_RE.search(text) and not has_value_signal:
        return "low-value-notification"
    if LOW_VALUE_LOG_RE.search(text) and not has_value_signal:
        return "low-value-log-or-tool-summary"
    if LOW_VALUE_PROGRESS_RE.search(text) and not has_value_signal:
        return "low-value-progress"
    return ""



_WORKFLOW_CONTINUATION_TOKENS = {
    "journal-first",
    "journal-digest",
    "journal",
    "digest",
    "merge/upsert",
    "merge",
    "upsert",
    "日记",
    "合并",
}


def _workflow_continuation_tokens(content: str, tags: set[str], entities: set[str]) -> set[str]:
    del content  # generated heuristic prefixes contain "Journal digest" for every candidate
    values: list[str] = []
    for tag in tags:
        clean = tag.lower()
        if clean.startswith("topic:"):
            values.append(clean.removeprefix("topic:"))
    values.extend(entity.lower() for entity in entities)
    haystack = "\n".join(values)
    return {token for token in _WORKFLOW_CONTINUATION_TOKENS if token in haystack}


def _is_workflow_continuation(candidate_tokens: set[str], existing_tokens: set[str]) -> bool:
    if candidate_tokens & existing_tokens:
        return True
    update_tokens = {"merge/upsert", "merge", "upsert", "合并"}
    journal_anchor_tokens = {"journal-first", "journal", "digest", "journal-digest", "日记"}
    return bool(candidate_tokens & update_tokens and existing_tokens & journal_anchor_tokens)


def _metadata_entities(metadata: dict[str, Any]) -> set[str]:
    raw = metadata.get("entities", []) if isinstance(metadata, dict) else []
    return {str(entity).strip() for entity in raw if str(entity).strip()}


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


def _candidate_rejection_reason(candidate: JournalDigestCandidate) -> str:
    if candidate.target not in JOURNAL_TARGETS:
        return "invalid-target"
    if len(candidate.content) < 40:
        return "too-short"
    if _looks_like_historical_template_noise(candidate.content):
        return "historical-template-noise"
    lowered = candidate.content.lower()
    if "operations workflow summary from journal digest:" in lowered or "journal digest memory" in lowered:
        return "historical-template-noise"
    capture_result = should_capture_text(candidate.content)
    if not capture_result.allowed:
        return f"capture-filter:{capture_result.reason or 'blocked'}"
    low_value_reason = _low_value_promotion_reason(candidate)
    if low_value_reason:
        return low_value_reason
    return ""


def _candidate_allowed(candidate: JournalDigestCandidate) -> bool:
    return not _candidate_rejection_reason(candidate)


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

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
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
                mark_entries_processed(
                    conn,
                    entry_ids=[int(entry_id) for entry_id in candidate.entry_ids],
                    run_id=run_id,
                    commit=False,
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

    started_outer_transaction = not conn.in_transaction
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
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
        if processed_entry_ids != expected_entry_ids:
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
            }
        mark_entries_processed(
            conn,
            entry_ids=sorted(expected_entry_ids),
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
    scope = normalize_scope_identity(scope, runtime_config)
    local_scope_id = build_scope_id(scope, runtime_config)
    shared_scope_id = build_shared_scope_id(scope, runtime_config)
    write_scope_ids = writable_scope_ids(scope, runtime_config)
    counts = Counter()
    pollution_counts = Counter()
    actions: list[dict[str, Any]] = []
    processed_entry_ids: set[int] = set()
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
                if source_checkpointed:
                    processed_entry_ids.update(
                        int(entry_id)
                        for component_candidate in component_candidates
                        for entry_id in component_candidate.entry_ids
                    )
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
            processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                _record_journal_rejection(
                    conn,
                    run_id=run_id,
                    entry_ids=candidate.entry_ids,
                    reason=(
                        "digest pollution: " + ",".join(pollution.reason_codes)
                    ),
                    candidate=candidate,
                )
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
                _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason=rejection_reason, candidate=candidate)
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
                _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason="existing memory covers candidate", candidate=candidate)
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
                # Store first, then attach journal provenance, then refresh the
                # vector companion. Vector repair can be retried from SQLite.
                _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                if not _defer_commits:
                    conn.commit()
                processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
                vector_event_id = _latest_current_vector_event_id(conn, stored_id)
                vector_replay = _replay_or_defer_journal_vector(
                    vector_runtime,
                    _deferred_vector_ops,
                    {
                        "event_id": vector_event_id,
                        "id": stored_id,
                        "source": "journal-digest",
                        "target": candidate.target,
                        "content": candidate.content,
                        "summary": summary,
                        "updated_at": updated_at,
                        "scope_id": candidate_scope_id,
                    },
                )
                if vector_event_id > 0:
                    actions[-1]["vector_event_id"] = vector_event_id
                    if _deferred_vector_ops is None:
                        actions[-1]["vector_replay"] = vector_replay
            else:
                counts["inserted"] -= 1
                counts["updated"] += 1
                actions.append({"action": "update", "reason": "duplicate store_row", "id": stored_id, "entry_ids": candidate.entry_ids})
                _merge_metadata(conn, memory_id=stored_id, candidate=candidate, run_id=run_id)
                _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                if not _defer_commits:
                    conn.commit()
                processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
    return {
        "counts": dict(counts),
        "pollution_counts": dict(pollution_counts),
        "actions": actions,
        "processed_entry_ids": sorted(processed_entry_ids),
    }




def _collect_journal_candidates(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    hermes_home: Path,
    scope: RuntimeScope,
    journal_config: dict[str, Any],
    requested_extractor: str,
) -> tuple[list[JournalDigestCandidate], str, str, Counter[str]]:
    if requested_extractor == "llm":
        fallback_allowed = _config_bool(journal_config, "allow_heuristic_fallback", False)
        try:
            candidates = llm_journal_candidates(conn, entries=entries, hermes_home=hermes_home, scope=scope, journal_config=journal_config)
            candidate_status_counts = Counter(getattr(candidates, "extractor_status_counts", {}) or {})
            if candidates:
                return candidates, "llm", "", candidate_status_counts
            if fallback_allowed:
                return heuristic_journal_candidates(entries), "heuristic-fallback", "llm produced no candidates", candidate_status_counts
            return candidates, "llm", "", candidate_status_counts
        except Exception as exc:
            if isinstance(exc, JournalDigestLLMError) and exc.error_kind in {
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
) -> list[RuntimeScope]:
    clean_excluded = sorted(excluded_chat_ids)
    exclusion_sql = ""
    params: list[object] = []
    if clean_excluded:
        placeholders = ",".join("?" for _ in clean_excluded)
        exclusion_sql = f" AND COALESCE(chat_id, '') NOT IN ({placeholders})"
        params.extend(clean_excluded)
    rows = conn.execute(
        f"""
        SELECT platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace, MIN(id) AS first_id
        FROM journal_entries
        WHERE (processed_run_id IS NULL OR processed_run_id = '')
          {exclusion_sql}
        GROUP BY scope_id
        ORDER BY first_id ASC
        LIMIT ?
        """,
        [*params, max(1, int(limit or 1000))],
    ).fetchall()
    return [_scope_from_row(row) for row in rows]


def _open_digest_connection(db_path: Path, *, dry_run: bool) -> sqlite3.Connection:
    """Open the digest SQLite connection without installing fallible setup.

    Caller ownership starts at the returned connection. Dry-run copies into a
    fresh in-memory destination; if that copy fails, the destination is closed
    before the error is re-raised so it cannot outlive this helper.
    """

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
    """Reset a failed transaction before persisting one best-effort receipt."""

    rollback_if_active(conn)
    ensure_journal_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO journal_digest_runs(
            id, started_at, finished_at, status, extractor, interval_label, error
        ) VALUES (?, ?, ?, 'error', ?, ?, ?)
        """,
        (
            run_id,
            started_at,
            now_iso(),
            extractor,
            interval_label,
            sanitize_report_text(str(error))[:1000],
        ),
    )
    conn.commit()



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
    threshold = _coerce_positive_int(journal_config.get("dynamic_backlog_threshold"), configured_limit * 4)
    if backlog <= threshold:
        return configured_limit
    default_ceiling = max(configured_limit, 500)
    ceiling = _coerce_positive_int(journal_config.get("max_entries_per_digest_ceiling"), default_ceiling)
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
    try:
        # Cleanup ownership starts here: every later config/schema/init
        # exception and every early return must close vector, conn, then lease.
        # Assign the opened connection before any fallible authorizer/schema
        # step so a later raise cannot leak a writable pager past lease release.
        conn = _open_digest_connection(db_path, dry_run=dry_run)
        if not dry_run:
            install_activation_lease_authorizer(conn, db_path)
        conn.row_factory = sqlite3.Row
        run_id = uuid.uuid4().hex
        started_at = now_iso()
        requested_extractor = str(extractor or "llm").strip().lower()
        extractor_used = requested_extractor
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
        active_scopes = (
            [scope]
            if scope is not None
            else _unprocessed_scopes(
                conn,
                limit=effective_limit,
                excluded_chat_ids=excluded_chat_ids,
            )
        )
        if not active_scopes:
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
        for active_scope in active_scopes:
            remaining = max(0, effective_limit - total_loaded_entries)
            if remaining <= 0:
                break
            active_scope = normalize_scope_identity(active_scope, runtime_config)
            scope_ids = accessible_scope_ids(active_scope, runtime_config)
            entries = load_unprocessed_journal_entries(
                conn,
                scope_ids=scope_ids,
                limit=remaining,
                excluded_chat_ids=excluded_chat_ids,
            )
            if not entries:
                continue
            total_loaded_entries += len(entries)
            prepare_network_boundary(conn, "journal.run_journal_digest.snapshot")
            try:
                collected: Any = _collect_journal_candidates(
                    conn,
                    entries=entries,
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
            except Exception as exc:
                if requested_extractor != "llm":
                    raise
                scope_extractor_used = "llm-error"
                _failure_reason, failure_meta = _quarantine_classification(exc)
                extractor_error = failure_meta
                scope_candidate_status_counts = Counter()
                candidates = []
                extraction_failure_count += 1
                pending_entry_ids = [int(entry.id) for entry in entries]
                actions.append(
                    {
                        "action": "error",
                        "reason": "extractor failure; source entries remain pending",
                        "entry_count": len(pending_entry_ids),
                        "entry_ids": pending_entry_ids[:20],
                        "classification": failure_meta,
                    }
                )
            extractor_counts[scope_extractor_used] += 1
            candidate_status_counts.update(scope_candidate_status_counts)
            if extractor_error:
                extractor_errors.append(extractor_error)
            if scope_extractor_used == "llm-error":
                continue
            total_candidates += len(candidates)
            candidate_entry_ids: set[int] = set()
            for candidate in candidates:
                for entry_id in candidate.entry_ids:
                    try:
                        candidate_entry_ids.add(int(entry_id))
                    except (TypeError, ValueError):
                        continue
            loaded_entry_ids = {int(entry.id) for entry in entries}
            if hasattr(candidates, "reviewed_entry_ids"):
                reviewed_entry_ids = {
                    int(entry_id)
                    for entry_id in getattr(candidates, "reviewed_entry_ids", set())
                } & loaded_entry_ids
                unresolved_entry_ids = {
                    int(entry_id)
                    for entry_id in getattr(candidates, "unresolved_entry_ids", set())
                } & loaded_entry_ids
            else:
                reviewed_entry_ids = set(loaded_entry_ids)
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
            )
            counts.update(applied["counts"])
            quarantine_counts.update(applied.get("pollution_counts", {}))
            applied_entry_ids = {int(entry_id) for entry_id in applied.get("processed_entry_ids", [])}
            unresolved_without_candidate_ids = sorted(
                unresolved_entry_ids - candidate_entry_ids
            )
            reviewed_without_candidate_ids = sorted(
                (reviewed_entry_ids - unresolved_entry_ids) - candidate_entry_ids
            )
            if unresolved_without_candidate_ids:
                actions.append(
                    {
                        "action": "pending",
                        "reason": "chunk extraction unresolved",
                        "entry_count": len(unresolved_without_candidate_ids),
                        "entry_ids": unresolved_without_candidate_ids[:20],
                    }
                )
            if reviewed_without_candidate_ids:
                counts["skipped"] += len(reviewed_without_candidate_ids)
                actions.append(
                    {
                        "action": "skip",
                        "reason": "no durable memory candidate",
                        "entry_count": len(reviewed_without_candidate_ids),
                        "entry_ids": reviewed_without_candidate_ids[:20],
                    }
                )
                if not dry_run:
                    _record_journal_rejection(
                        conn,
                        run_id=run_id,
                        entry_ids=reviewed_without_candidate_ids,
                        reason="no durable memory candidate",
                        candidate=JournalDigestCandidate(
                            content="No durable memory candidate was produced for this reviewed journal entry.",
                            target="memory",
                            entry_ids=reviewed_without_candidate_ids,
                        ),
                    )
            scope_done_ids = sorted(applied_entry_ids | set(reviewed_without_candidate_ids))
            processed_entry_ids.extend(scope_done_ids)
            if not dry_run and scope_done_ids:
                mark_entries_processed(
                    conn, entry_ids=scope_done_ids, run_id=run_id
                )
            actions.extend(applied["actions"])
            if vector_runtime is not None:
                # Do not swallow companion teardown. A close failure must enter
                # the outer handler so final cleanup can still close truth and
                # release the lease, then surface the vector error.
                vector_runtime.close()
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
            mark_entries_processed(conn, entry_ids=unique_processed_entry_ids, run_id=run_id)
            pruned_entries = _prune_processed_journal(conn, retention_days=retention_days)
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
            conn.execute(
                """
                INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, interval_label,
                    processed_entries, inserted, updated, skipped, error, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    started_at,
                    now_iso(),
                    run_status,
                    extractor_used,
                    interval_label,
                    len(unique_processed_entry_ids),
                    counts.get("inserted", 0),
                    counts.get("updated", 0),
                    counts.get("skipped", 0),
                    run_error or None,
                    json.dumps(
                        journal_digest_metadata(
                            total_candidates=total_candidates,
                            total_loaded_entries=total_loaded_entries,
                            actions=actions,
                            requested_extractor=requested_extractor,
                            extractor_used=extractor_used,
                            extractor_counts=extractor_counts,
                            extractor_errors=extractor_errors,
                            quarantine_counts=quarantine_counts,
                            backlog_before=backlog_before,
                            effective_limit=effective_limit,
                            retention_days=retention_days,
                            pruned_entries=pruned_entries,
                            backlog_after=receipt_fields["backlog_after"],
                            productive_writes=receipt_fields["productive_writes"],
                            no_insert_reason=receipt_fields["no_insert_reason"],
                            health_flags=receipt_fields["health_flags"],
                            recommended_next_limit=receipt_fields["recommended_next_limit"],
                            candidate_status_counts=candidate_status_counts,
                        ),
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
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
        )
        if extraction_failure_count:
            result["ok"] = False
            result["status"] = "error"
            result["error"] = run_error
        return result
    except Exception as exc:
        if conn is not None:
            try:
                if dry_run:
                    rollback_if_active(conn)
                else:
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
        if vector_runtime is not None:
            try:
                vector_runtime.close()
            except Exception as exc:
                vector_close_error = exc
        # Vector is rebuildable. Truth SQLite close is authoritative: a close
        # failure must surface and must not reach lease release while a
        # writable pager may still be live. After truth and lease succeed,
        # a captured vector close error is still raised so teardown is visible.
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
