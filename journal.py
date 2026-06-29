from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .capture_filters import sanitize_report_text, should_capture_text
from .digest_run_results import journal_digest_metadata, journal_digest_success_result, no_unprocessed_journal_result
from .gating import clean_text, compact_text, dedup_key
from .governance import is_conflicting, merge_memory_text, semantic_similarity
from .models import RuntimeScope
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
from .sql_store import ensure_schema, now_iso, store_row, update_row
from .vector_runtime import upsert_vector_record

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

JOURNAL_TARGETS = {"user", "memory", "project", "ops"}



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
        SELECT id, content, metadata
        FROM memories
        WHERE scope_id IN ({placeholders}) AND target = ?
        ORDER BY updated_at DESC
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
        content = str(row["content"])
        if dedup_key(content) == candidate_key:
            return str(row["id"]), content, 1.0
        score = semantic_similarity(content, candidate.content)
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except Exception:
            metadata = {}
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
        existing_values = existing.get(key) if isinstance(existing.get(key), list) else []
        incoming_values = incoming.get(key) if isinstance(incoming.get(key), list) else []
        merged = _unique([*map(str, existing_values), *map(str, incoming_values)], limit=240 if key == "journal_entry_ids" else 40)
        if merged:
            existing[key] = merged
    for key in ("journal_run_id", "journal_reason", "memory_type"):
        if incoming.get(key):
            existing[key] = incoming[key]
    existing["importance"] = max(float(existing.get("importance") or 0.0), float(incoming.get("importance") or 0.0))
    existing["confidence"] = max(float(existing.get("confidence") or 0.0), float(incoming.get("confidence") or 0.0))
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


def apply_journal_candidates(
    conn: sqlite3.Connection,
    vector_runtime: Any,
    scope: RuntimeScope,
    *,
    run_id: str,
    candidates: list[JournalDigestCandidate],
    dry_run: bool = False,
    runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope = normalize_scope_identity(scope, runtime_config)
    scope_ids = accessible_scope_ids(scope, runtime_config)
    write_scope_ids = writable_scope_ids(scope, runtime_config)
    shared_scope_id = build_shared_scope_id(scope, runtime_config)
    counts = Counter()
    actions: list[dict[str, Any]] = []
    processed_entry_ids: set[int] = set()
    for candidate in candidates:
        rejection_reason = _candidate_rejection_reason(candidate)
        if rejection_reason:
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": rejection_reason, "entry_ids": candidate.entry_ids})
            processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason=rejection_reason, candidate=candidate)
                conn.commit()
            continue
        match_id, match_content, score = _find_match(conn, scope_ids, candidate)
        match_scope_id = _memory_scope_id(conn, match_id) if match_id else ""
        match_is_writable = bool(match_scope_id and match_scope_id in set(write_scope_ids))
        if match_id and score >= 0.88:
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": "existing memory covers candidate", "id": match_id, "score": round(score, 4), "entry_ids": candidate.entry_ids})
            processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
            if not dry_run:
                _record_journal_rejection(conn, run_id=run_id, entry_ids=candidate.entry_ids, reason="existing memory covers candidate", candidate=candidate)
                conn.commit()
            continue
        if match_id and match_is_writable and score >= 0.55 and not is_conflicting(match_content, candidate.content):
            merged = merge_memory_text(match_content, candidate.content)
            if candidate.content not in merged and "merge/upsert" in candidate.content.lower():
                merged = f"{merged}\n§\n{candidate.content}"
            counts["updated"] += 1
            actions.append({"action": "update", "id": match_id, "score": round(score, 4), "entry_ids": candidate.entry_ids})
            if not dry_run:
                updated, summary, updated_at = update_row(
                    conn,
                    memory_id=match_id,
                    content=merged,
                    target=candidate.target,
                    scope_ids=write_scope_ids,
                )
                if updated:
                    _merge_metadata(conn, memory_id=match_id, candidate=candidate, run_id=run_id)
                    _record_journal_sources(conn, memory_id=match_id, run_id=run_id, entry_ids=candidate.entry_ids)
                    conn.commit()
                    processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
                    if vector_runtime is not None:
                        row = conn.execute("SELECT scope_id FROM memories WHERE id = ?", (match_id,)).fetchone()
                        row_scope_id = str(row["scope_id"] if row else shared_scope_id)
                        upsert_vector_record(
                            vector_runtime,
                            id=match_id,
                            source="journal-digest",
                            target=candidate.target,
                            content=merged,
                            summary=summary,
                            updated_at=updated_at,
                            scope_id=row_scope_id,
                        )
            continue
        memory_id = uuid.uuid4().hex
        counts["inserted"] += 1
        actions.append({"action": "insert", "id": memory_id, "target": candidate.target, "entry_ids": candidate.entry_ids})
        if not dry_run:
            stored_id, summary, updated_at, inserted = store_row(
                conn,
                memory_id=memory_id,
                scope_id=shared_scope_id,
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
                metadata=json.dumps({**_cross_platform_metadata(scope, runtime_config), **candidate_metadata(candidate, run_id)}, ensure_ascii=False, sort_keys=True),
            )
            if inserted:
                _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                conn.commit()
                processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
                if vector_runtime is not None:
                    upsert_vector_record(
                        vector_runtime,
                        id=stored_id,
                        source="journal-digest",
                        target=candidate.target,
                        content=candidate.content,
                        summary=summary,
                        updated_at=updated_at,
                        scope_id=shared_scope_id,
                    )
            else:
                counts["inserted"] -= 1
                counts["updated"] += 1
                actions.append({"action": "update", "reason": "duplicate store_row", "id": stored_id, "entry_ids": candidate.entry_ids})
                _merge_metadata(conn, memory_id=stored_id, candidate=candidate, run_id=run_id)
                _record_journal_sources(conn, memory_id=stored_id, run_id=run_id, entry_ids=candidate.entry_ids)
                conn.commit()
                processed_entry_ids.update(int(entry_id) for entry_id in candidate.entry_ids)
    return {"counts": dict(counts), "actions": actions, "processed_entry_ids": sorted(processed_entry_ids)}




def _collect_journal_candidates(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    hermes_home: Path,
    scope: RuntimeScope,
    journal_config: dict[str, Any],
    requested_extractor: str,
) -> tuple[list[JournalDigestCandidate], str, str]:
    if requested_extractor == "llm":
        fallback_allowed = _config_bool(journal_config, "allow_heuristic_fallback", False)
        try:
            candidates = llm_journal_candidates(conn, entries=entries, hermes_home=hermes_home, scope=scope, journal_config=journal_config)
            if candidates:
                return candidates, "llm", ""
            if fallback_allowed:
                return heuristic_journal_candidates(entries), "heuristic-fallback", "llm produced no candidates"
            return [], "llm", "llm produced no candidates"
        except Exception as exc:
            if isinstance(exc, JournalDigestLLMError) and exc.error_kind in {"parse", "filtered"}:
                raise
            if fallback_allowed:
                try:
                    return heuristic_journal_candidates(entries), "heuristic-fallback", "llm failed; heuristic fallback enabled"
                except Exception:
                    pass
            raise
    return heuristic_journal_candidates(entries), "heuristic", ""


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


def _unprocessed_scopes(conn: sqlite3.Connection, *, limit: int = 1000) -> list[RuntimeScope]:
    rows = conn.execute(
        """
        SELECT platform, user_id, chat_id, thread_id, gateway_session_key, agent_identity, agent_workspace, MIN(id) AS first_id
        FROM journal_entries
        WHERE processed_run_id IS NULL OR processed_run_id = ''
        GROUP BY scope_id
        ORDER BY first_id ASC
        LIMIT ?
        """,
        (max(1, int(limit or 1000)),),
    ).fetchall()
    return [_scope_from_row(row) for row in rows]


def _open_digest_connection(db_path: Path, *, dry_run: bool) -> sqlite3.Connection:
    if dry_run:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        if db_path.exists():
            source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                source.backup(conn)
            finally:
                source.close()
        return conn
    return sqlite3.connect(db_path, timeout=30)



def _dynamic_journal_digest_limit(conn: sqlite3.Connection, *, configured_limit: int, journal_config: dict[str, Any]) -> int:
    if not _config_bool(journal_config, "dynamic_max_entries_enabled", True):
        return configured_limit
    backlog = _journal_unprocessed_count(conn)
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
) -> dict[str, Any]:
    hermes_home = hermes_home.expanduser().resolve()
    storage_dir = hermes_home / "scope-recall"
    if not dry_run:
        storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "memory.sqlite3"
    conn = _open_digest_connection(db_path, dry_run=dry_run)
    conn.row_factory = sqlite3.Row
    run_id = uuid.uuid4().hex
    started_at = now_iso()
    vector_runtime = None
    runtime_config = _runtime_config(hermes_home)
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
    configured_limit = _coerce_positive_int(journal_config.get("max_entries_per_digest"), 500)
    effective_limit = _coerce_positive_int(limit_entries, configured_limit) if limit_entries is not None else configured_limit
    retention_days = int(journal_config.get("retention_days") or 0)
    requested_extractor = str(extractor or journal_config.get("extractor") or "llm").strip().lower()
    extractor_used = requested_extractor
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        if limit_entries is None:
            effective_limit = _dynamic_journal_digest_limit(conn, configured_limit=configured_limit, journal_config=journal_config)
        backlog_before = _journal_unprocessed_count(conn)
        active_scopes = [scope] if scope is not None else _unprocessed_scopes(conn, limit=effective_limit)
        if not active_scopes:
            return no_unprocessed_journal_result(run_id=run_id, requested_extractor=requested_extractor, extractor_used=extractor_used)

        total_loaded_entries = 0
        total_candidates = 0
        processed_entry_ids: list[int] = []
        counts = Counter()
        extractor_counts = Counter()
        quarantine_counts = Counter()
        extractor_errors: list[Any] = []
        actions: list[dict[str, Any]] = []
        for active_scope in active_scopes:
            remaining = max(0, effective_limit - total_loaded_entries)
            if remaining <= 0:
                break
            active_scope = normalize_scope_identity(active_scope, runtime_config)
            scope_ids = accessible_scope_ids(active_scope, runtime_config)
            entries = load_unprocessed_journal_entries(conn, scope_ids=scope_ids, limit=remaining)
            if not entries:
                continue
            total_loaded_entries += len(entries)
            try:
                candidates, scope_extractor_used, extractor_error = _collect_journal_candidates(
                    conn,
                    entries=entries,
                    hermes_home=hermes_home,
                    scope=active_scope,
                    journal_config=journal_config,
                    requested_extractor=requested_extractor,
                )
            except Exception as exc:
                if requested_extractor != "llm":
                    raise
                scope_extractor_used = "llm-quarantine"
                quarantine_reason, quarantine_meta = _quarantine_classification(exc)
                extractor_error = quarantine_meta
                candidates = []
                quarantine_entry_ids = [int(entry.id) for entry in entries]
                counts["skipped"] += len(quarantine_entry_ids)
                quarantine_counts[str(quarantine_meta["classification"])] += len(quarantine_entry_ids)
                actions.append(
                    {
                        "action": "skip",
                        "reason": quarantine_reason,
                        "entry_count": len(quarantine_entry_ids),
                        "entry_ids": quarantine_entry_ids[:20],
                        "classification": quarantine_meta,
                    }
                )
                if not dry_run:
                    _quarantine_journal_entries(
                        conn,
                        run_id=run_id,
                        entries=entries,
                        reason=quarantine_reason,
                        error=exc,
                    )
                processed_entry_ids.extend(quarantine_entry_ids)
            extractor_counts[scope_extractor_used] += 1
            if extractor_error:
                extractor_errors.append(extractor_error)
            if scope_extractor_used == "llm-quarantine":
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
            applied = apply_journal_candidates(conn, vector_runtime, active_scope, run_id=run_id, candidates=candidates, dry_run=dry_run, runtime_config=runtime_config)
            counts.update(applied["counts"])
            applied_entry_ids = {int(entry_id) for entry_id in applied.get("processed_entry_ids", [])}
            reviewed_without_candidate_ids = sorted(loaded_entry_ids - candidate_entry_ids)
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
            processed_entry_ids.extend(sorted(applied_entry_ids | set(reviewed_without_candidate_ids)))
            actions.extend(applied["actions"])
            if vector_runtime is not None:
                try:
                    vector_runtime.close()
                except Exception:
                    pass
                vector_runtime = None

        if total_loaded_entries == 0:
            return no_unprocessed_journal_result(run_id=run_id, requested_extractor=requested_extractor, extractor_used=extractor_used)
        unique_processed_entry_ids = sorted(set(processed_entry_ids))
        if extractor_counts:
            extractor_used = next(iter(extractor_counts)) if len(extractor_counts) == 1 else "mixed"
        else:
            extractor_used = requested_extractor
        pruned_entries = 0
        if not dry_run:
            mark_entries_processed(conn, entry_ids=unique_processed_entry_ids, run_id=run_id)
            pruned_entries = _prune_processed_journal(conn, retention_days=retention_days)
            conn.execute(
                """
                INSERT INTO journal_digest_runs(id, started_at, finished_at, status, extractor, interval_label,
                    processed_entries, inserted, updated, skipped, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    started_at,
                    now_iso(),
                    "ok",
                    extractor_used,
                    interval_label,
                    len(unique_processed_entry_ids),
                    counts.get("inserted", 0),
                    counts.get("updated", 0),
                    counts.get("skipped", 0),
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
                        ),
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
        return journal_digest_success_result(
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
        )
    except Exception as exc:
        if not dry_run:
            ensure_journal_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO journal_digest_runs(id, started_at, finished_at, status, extractor, interval_label, error)
                VALUES (?, ?, ?, 'error', ?, ?, ?)
                """,
                (run_id, started_at, now_iso(), requested_extractor, interval_label, sanitize_report_text(str(exc)[:1000])),
            )
            conn.commit()
        raise
    finally:
        if vector_runtime is not None:
            try:
                vector_runtime.close()
            except Exception:
                pass
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Digest scope-recall journal entries into high-quality durable memories")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--extractor", choices=["llm", "heuristic"], default="llm", help="Extraction backend; default is LLM-first. Use heuristic only as an explicit operator fallback.")
    parser.add_argument("--interval-label", default="manual", help="Human-readable schedule label, e.g. 2h")
    parser.add_argument("--limit-entries", type=int, default=None, help="Maximum unprocessed journal entries per run; defaults to journal.max_entries_per_digest")
    parser.add_argument("--provider", default="", help="LLM provider name from Hermes config, e.g. deepseek; overrides main model provider for this digest run")
    parser.add_argument("--model", default="", help="LLM model for this digest run")
    parser.add_argument("--api-mode", default="", choices=["", "chat_completions", "codex_responses"], help="LLM API mode for this digest run")
    parser.add_argument("--base-url", default="", help="LLM base URL for this digest run")
    parser.add_argument("--endpoint", default="", help="Full chat-completions endpoint override for this digest run")
    parser.add_argument("--key-env", default="", help="Environment variable name containing the LLM API key")
    parser.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    append_group = parser.add_mutually_exclusive_group()
    append_group.add_argument("--append-v1", dest="append_v1", action="store_true", default=None, help="Append /v1 before /chat/completions when using base-url")
    append_group.add_argument("--no-append-v1", dest="append_v1", action="store_false", help="Do not append /v1 before /chat/completions when using base-url")
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
        )
        result["elapsed_seconds"] = round(time.time() - started, 3)
        if args.verbose or args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            compact = {key: result.get(key) for key in ("ok", "status", "processed_entries", "candidates", "inserted", "updated", "skipped")}
            print(json.dumps(compact, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": sanitize_report_text(str(exc))}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
