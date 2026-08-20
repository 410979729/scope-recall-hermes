"""Extractor selection and shared runtime helpers for heuristic and LLM journal digest paths.

This module bridges raw journal bundles to candidate objects without committing durable memory writes."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import load_runtime_config
from .gating import compact_text
from .http_utils import explicit_insecure_endpoint_opt_in
from .journal_candidates import JournalDigestCandidate, _unique
from .digest_state import active_journal_digest_llm_error, journal_scope_session_identity
from .journal_llm import JournalDigestLLMError, _call_llm_with_retries, _quarantine_classification
from .journal_store import JournalEntry, _journal_entry_for_digest, journal_entry_group_identity
from .models import RuntimeScope
from .nightly_digest import (
    DigestOptions,
    MessageRecord,
    ScopeProfile,
    SessionBundle,
    _existing_context_target_ids,
    _existing_context_target_ids_by_scope,
    _parse_llm_candidates_with_status,
    build_prompt,
    existing_memory_context,
    resolve_llm_config,
    session_chunks,
)
from .scope import accessible_scope_ids, build_scope_id, build_shared_scope_id, normalize_scope_identity
from .transaction_guard import prepare_network_boundary, release_snapshot_transaction

__all__ = [
    "_coerce_nonnegative_float",
    "_coerce_positive_int",
    "_config_bool",
    "_journal_from_digest_candidate",
    "_journal_runtime_config",
    "_journal_session_bundles",
    "_parse_entry_timestamp",
    "_runtime_config",
    "llm_journal_candidates",
]


class JournalCandidateList(list[JournalDigestCandidate]):
    """Candidates plus chunk-level review/checkpoint provenance."""

    def __init__(
        self,
        values: list[JournalDigestCandidate],
        *,
        extractor_status_counts: Counter[str] | None = None,
        reviewed_entry_ids: set[int] | None = None,
        unresolved_entry_ids: set[int] | None = None,
        retryable_unresolved_entry_ids: set[int] | None = None,
        deferred_entry_ids: set[int] | None = None,
        attempted_entry_ids: set[int] | None = None,
        extractor_error: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(values)
        self.extractor_status_counts = Counter(extractor_status_counts or {})
        self.reviewed_entry_ids = set(reviewed_entry_ids or set())
        self.unresolved_entry_ids = set(unresolved_entry_ids or set())
        # Transient provider failures must stay pending without consuming the
        # entry's bounded extraction attempts (issue #45's failure classes).
        self.retryable_unresolved_entry_ids = set(
            retryable_unresolved_entry_ids or set()
        )
        # Entries loaded into the run but pushed past the per-session chunk
        # budget. The store persists them as deferred overflow and advances the
        # session resume cursor so the next run starts after the covered prefix.
        self.deferred_entry_ids = set(deferred_entry_ids or set())
        # IDs actually passed to ``_call_llm_with_retries``. Orchestration may
        # increment or reset durable retryable counters only for this set.
        self.attempted_entry_ids = {
            int(entry_id) for entry_id in (attempted_entry_ids or set())
        }
        # Sanitized all-chunk failure metadata. Returning this on the list
        # keeps review/defer/retryable sets instead of raising and discarding
        # them. Callers still treat an all-timeout run as a visible error.
        self.extractor_error = (
            dict(extractor_error) if isinstance(extractor_error, dict) else None
        )


def _parse_entry_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _digestible_tool_ids(entries: list[JournalEntry]) -> set[int]:
    """Return tool rows still in the digestible window (not admission noise)."""

    ids: set[int] = set()
    for entry in entries:
        if str(getattr(entry, "role", "") or "").strip().lower() != "tool":
            continue
        try:
            ids.add(int(entry.id))
        except (TypeError, ValueError, AttributeError):
            continue
    return ids


def _bundle_scope_session_identity(bundle: SessionBundle) -> tuple[str, str] | None:
    """Return the stored composite identity carried on one journal bundle."""

    return journal_scope_session_identity(
        SimpleNamespace(scope_id=bundle.scope_id, session_id=bundle.id)
    )


def _exact_covered_tool_ids(
    entries: list[JournalEntry],
    *,
    identity: tuple[str, str] | None,
    chunk_ids: set[int],
) -> list[int] | None:
    """Return tool ids that sit inside this chunk's own covered message span.

    Coverage is the chunk's message ids plus the same stored
    ``(scope_id, session_id)`` pair. A missing identity or empty chunk fails
    closed so callers cannot reconstruct a global ID window.
    """

    if identity is None or not chunk_ids:
        return None
    cover_lo = min(chunk_ids)
    cover_hi = max(chunk_ids)
    tool_ids: list[int] = []
    for entry in entries:
        if str(getattr(entry, "role", "") or "").strip().lower() != "tool":
            continue
        if journal_scope_session_identity(entry) != identity:
            continue
        try:
            entry_id = int(entry.id)
        except (TypeError, ValueError, AttributeError):
            continue
        if cover_lo <= entry_id <= cover_hi:
            tool_ids.append(entry_id)
    return tool_ids


def _uncovered_digestible_tool_ids(
    entries: list[JournalEntry],
    *,
    identity: tuple[str, str] | None,
    covered_entry_ids: set[int],
) -> set[int]:
    """Defer digestible tools that sit past the covered attempt prefix.

    Tool rows are omitted from ``SessionBundle.messages``, so chunk coverage
    never lists them. A tool after the last attempted message is an
    unattempted suffix, not candidate provenance. Identity uses the stored
    ``(scope_id, session_id)`` pair so a same-named session in another scope
    cannot donate or steal suffix tools.
    """

    if identity is None:
        return set()
    cover_hi = max(covered_entry_ids) if covered_entry_ids else 0
    deferred: set[int] = set()
    for entry in entries:
        if str(getattr(entry, "role", "") or "").strip().lower() != "tool":
            continue
        if journal_scope_session_identity(entry) != identity:
            continue
        try:
            entry_id = int(entry.id)
        except (TypeError, ValueError, AttributeError):
            continue
        if not covered_entry_ids or entry_id > cover_hi:
            deferred.add(entry_id)
    return deferred


def _journal_session_bundles(entries: list[JournalEntry]) -> list[SessionBundle]:
    """Build session bundles from raw journal rows for candidate extraction.

    Bundling preserves the stored ``(scope_id, session_id)`` pair so the same
    textual session_id in another scope cannot merge into one LLM window.
    Empty session_id stays empty; it is not remapped to the display label
    ``unknown``.
    """
    grouped: dict[tuple[str, str], list[JournalEntry]] = {}
    for entry in entries:
        grouped.setdefault(journal_entry_group_identity(entry), []).append(entry)
    bundles: list[SessionBundle] = []
    for (scope_id, session_id), session_entries in grouped.items():
        session_entries.sort(key=lambda item: (item.turn_number, item.id))
        digest_entries = [entry for entry in (_journal_entry_for_digest(item) for item in session_entries) if entry is not None]
        if not digest_entries:
            continue
        original_roles = {entry.role for entry in digest_entries}
        messages: list[MessageRecord] = []
        tool_names: list[str] = []
        for entry in digest_entries:
            if entry.role == "tool":
                tool_name = str(entry.metadata.get("tool_name") or "").strip()
                if tool_name:
                    tool_names.append(tool_name)
                continue
            role = entry.role if entry.role in {"user", "assistant"} else "assistant"
            content = entry.content
            messages.append(
                MessageRecord(
                    id=entry.id,
                    session_id=entry.session_id,
                    role=role,
                    content=content,
                    timestamp=_parse_entry_timestamp(entry.created_at),
                    tool_name=str(entry.metadata.get("tool_name") or ""),
                )
            )
        if not messages or not any(message.role == "user" for message in messages):
            if original_roles == {"tool"}:
                bundles.append(
                    SessionBundle(
                        id=session_id,
                        source="journal-tool-only",
                        title=session_id,
                        messages=[],
                        tool_names=_unique(tool_names, limit=24),
                        is_task=bool(tool_names),
                        completed=False,
                        scope_id=scope_id,
                    )
                )
            continue
        title = compact_text(next((message.content for message in messages if message.role == "user"), session_id), 100)
        text = "\n".join(message.content for message in messages).lower()
        is_task = bool(tool_names) or any(token in text for token in ["fix", "debug", "deploy", "release", "verify", "修", "排障", "部署", "验证", "实现"])
        original_roles = {entry.role for entry in digest_entries}
        bundles.append(
            SessionBundle(
                id=session_id,
                source="journal-tool-only" if original_roles == {"tool"} else "journal",
                title=title,
                messages=messages,
                tool_names=_unique(tool_names, limit=24),
                is_task=is_task,
                completed=any(token in text for token in ["passed", "通过", "完成", "验证"]),
                scope_id=scope_id,
            )
        )
    return bundles


def _journal_from_digest_candidate(
    candidate: Any,
    *,
    covered_tool_ids: list[int] | None = None,
) -> JournalDigestCandidate:
    exact: list[int] | None = None
    if covered_tool_ids is not None:
        exact = []
        for item in covered_tool_ids:
            try:
                exact.append(int(item))
            except (TypeError, ValueError):
                continue
    return JournalDigestCandidate(
        content=str(candidate.content),
        target=str(candidate.target or "memory"),
        memory_type=str(candidate.memory_type or "summary"),
        importance=float(candidate.importance or 0.55),
        confidence=float(candidate.confidence or 0.65),
        entities=list(candidate.entities or []),
        tags=_unique([*list(candidate.tags or []), "journal-digest", "llm-digest"], limit=20),
        reason=str(candidate.reason or "llm journal digest extraction"),
        entry_ids=[int(item) for item in list(candidate.message_ids or [])],
        session_ids=[str(candidate.session_id)] if getattr(candidate, "session_id", "") else [],
        evolution=getattr(candidate, "evolution", None),
        covered_tool_ids=exact,
    )


def _parse_journal_llm_candidates(
    raw: str,
    *,
    bundle: SessionBundle,
    scope_id: str = "",
    shared_scope_id: str = "",
    allowed_target_ids: set[str] | None = None,
    allowed_target_ids_by_scope: dict[str, set[str]] | None = None,
    allowed_message_ids: set[int] | None = None,
) -> tuple[list[Any], str]:
    candidates, status = _parse_llm_candidates_with_status(
        raw,
        bundle=bundle,
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        allowed_target_ids=allowed_target_ids,
        allowed_target_ids_by_scope=allowed_target_ids_by_scope,
        allowed_message_ids=allowed_message_ids,
    )
    if status == "parsed":
        return candidates, status
    if status in {"empty", "explicit_skip", "filtered"}:
        return [], status
    error_kind = "parse" if status == "parse" else "filtered"
    raise active_journal_digest_llm_error()(
        f"{error_kind} after 1 attempt(s): LLM digest output status={status}",
        attempts=1,
        error_kind=error_kind,
        retryable=False,
    )


def _runtime_config(hermes_home: Path) -> dict[str, Any]:
    plugin_dir = Path(__file__).resolve().parent
    storage_dir = hermes_home / "scope-recall"
    return load_runtime_config(plugin_dir, storage_dir)


def _journal_runtime_config(hermes_home: Path) -> dict[str, Any]:
    config = _runtime_config(hermes_home)
    raw_journal = config.get("journal")
    return raw_journal if isinstance(raw_journal, dict) else {}


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _coerce_nonnegative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, parsed)


def _config_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _safe_chunk_llm_error() -> JournalDigestLLMError:
    """Bounded fallback that never echoes raw exception, prompt, or path text."""

    return JournalDigestLLMError(
        "unknown after 1 attempt(s)",
        attempts=1,
        error_kind="unknown",
        retryable=True,
    )


def _normalize_chunk_exception(exc: Exception) -> JournalDigestLLMError:
    """Map a per-chunk failure onto sanitized chunk-error metadata.

    Incoming structured errors are preserved. Generic exceptions reuse the
    existing classifier for kind/retryability, but the public message stays
    limited to kind plus exception type so receipt metadata cannot leak
    prompt, response, scope, path, or secret text. Classifier or sanitizer
    failures fall back to a retryable unknown error instead of bare-raising.
    """

    if isinstance(exc, JournalDigestLLMError):
        return exc
    error_type: type[JournalDigestLLMError] = JournalDigestLLMError
    try:
        looked_up = active_journal_digest_llm_error()
        if isinstance(exc, looked_up):
            return exc
        error_type = looked_up
    except Exception:
        error_type = JournalDigestLLMError
    kind = "unknown"
    retryable = True
    attempts = 1
    try:
        _reason, meta = _quarantine_classification(exc)
        kind = str(meta.get("kind") or "unknown") or "unknown"
        retryable = bool(meta.get("retryable"))
        attempts = max(1, int(meta.get("attempts") or 1))
    except Exception:
        kind, retryable, attempts = "unknown", True, 1
    try:
        return error_type(
            f"{kind} after {attempts} attempt(s): {type(exc).__name__}",
            attempts=attempts,
            error_kind=kind,
            retryable=retryable,
        )
    except Exception:
        return _safe_chunk_llm_error()


def _safe_extractor_error_meta() -> dict[str, Any]:
    """Receipt-safe all-chunk failure metadata when classification itself fails."""

    return {
        "classification": "retry_exhausted",
        "kind": "unknown",
        "retryable": True,
        "attempts": 1,
        "message": "unknown",
    }


def llm_journal_candidates(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    hermes_home: Path,
    scope: RuntimeScope,
    journal_config: dict[str, Any],
) -> list[JournalDigestCandidate]:
    """Extract journal candidates with an LLM and return status-rich results.

    The function preserves quarantine/fallback information so failed model
    calls remain visible operational debt. Before any LLM attempt, missing
    metadata stays fail-closed at zero. Once any physical ID has reached
    ``_call_llm_with_retries``, a later generic ``Exception`` is normalized
    in this function instead of bare-raising and discarding the prefix.
    """
    runtime_config = _runtime_config(hermes_home)
    options = DigestOptions(
        hermes_home=hermes_home,
        digest_date=datetime.now(timezone.utc).date(),
        extractor="llm",
        chunk_chars=_coerce_positive_int(journal_config.get("llm_chunk_chars"), 7000),
        max_session_chars=_coerce_positive_int(journal_config.get("llm_max_session_chars"), 16000),
        provider=str(journal_config.get("provider") or journal_config.get("llm_provider") or ""),
        model=str(journal_config.get("model") or ""),
        base_url=str(journal_config.get("base_url") or ""),
        endpoint=str(journal_config.get("endpoint") or journal_config.get("chat_endpoint") or ""),
        append_v1=_config_bool(journal_config, "append_v1", True) if "append_v1" in journal_config else None,
        allow_insecure_endpoint=(
            explicit_insecure_endpoint_opt_in(
                journal_config.get("allow_insecure_endpoint")
            )
            if "allow_insecure_endpoint" in journal_config
            else None
        ),
        api_key=str(journal_config.get("api_key") or ""),
        api_key_env=str(journal_config.get("api_key_env") or journal_config.get("key_env") or ""),
        api_mode=str(journal_config.get("api_mode") or ""),
        timeout=float(journal_config.get("timeout") or journal_config.get("llm_timeout") or 60.0),
    )
    llm_config = resolve_llm_config(hermes_home, options)
    active_scope = normalize_scope_identity(scope, runtime_config)
    profile = ScopeProfile(
        scope=active_scope,
        scope_id=build_scope_id(active_scope, runtime_config),
        shared_scope_id=build_shared_scope_id(active_scope, runtime_config),
        accessible_scope_ids=accessible_scope_ids(active_scope, runtime_config),
    )
    existing = existing_memory_context(conn, profile)
    allowed_target_ids = _existing_context_target_ids(existing)
    allowed_target_ids_by_scope = _existing_context_target_ids_by_scope(conn, profile)
    release_snapshot_transaction(conn)
    prepare_network_boundary(conn, "journal.llm_journal_candidates.snapshot")
    output: list[JournalDigestCandidate] = []
    reviewed_entry_ids: set[int] = set()
    unresolved_entry_ids: set[int] = set()
    retryable_unresolved_entry_ids: set[int] = set()
    deferred_entry_ids: set[int] = set()
    attempted_entry_ids: set[int] = set()
    max_attempts = _coerce_positive_int(journal_config.get("llm_max_attempts") or journal_config.get("llm_retry_attempts"), 3)
    retry_delay = _coerce_nonnegative_float(journal_config.get("llm_retry_delay"), 1.0)
    chunk_errors: list[JournalDigestLLMError] = []
    extractor_status_counts: Counter[str] = Counter()
    attempted_chunks = 0
    bundles = _journal_session_bundles(entries)
    exposed_entry_ids = {
        int(message.id)
        for bundle in bundles
        for message in bundle.messages
    }
    digestible_tool_ids = _digestible_tool_ids(entries)
    reviewed_entry_ids.update(
        int(entry.id)
        for entry in entries
        if int(entry.id) not in exposed_entry_ids
        and int(entry.id) not in digestible_tool_ids
    )
    for bundle in bundles:
        bundle_identity = _bundle_scope_session_identity(bundle)
        bundle_entry_ids = {int(message.id) for message in bundle.messages}
        if bundle.source == "journal-tool-only":
            # Tool-only sessions are deliberately not LLM-extracted; leaving
            # them unmarked made them reload on every digest run forever
            # (issue #46's ghost-state family). Reviewed-without-candidate is
            # their honest terminal state; experience promotion reads journal
            # rows independently of processed markers.
            reviewed_entry_ids.update(bundle_entry_ids)
            reviewed_entry_ids.update(
                int(entry.id)
                for entry in entries
                if bundle_identity is not None
                and journal_scope_session_identity(entry) == bundle_identity
                and int(entry.id) in digestible_tool_ids
            )
            continue
        covered_entry_ids: set[int] = set()
        for chunk in session_chunks(bundle, chunk_chars=options.chunk_chars, max_session_chars=options.max_session_chars):
            chunk_ids = {int(value) for value in chunk.message_ids}
            chunk_attempted = False
            try:
                prompt = build_prompt(
                    bundle,
                    chunk,
                    existing,
                    retention_profile=str(journal_config.get("retention_profile") or "balanced"),
                )
                release_snapshot_transaction(conn)
                prepare_network_boundary(conn, "journal.llm_journal_candidates.llm")
                # Charge only IDs that actually reach the LLM helper. A later
                # pre-call failure must not invent attempted metadata.
                attempted_entry_ids.update(chunk_ids)
                chunk_attempted = True
                attempted_chunks += 1
                covered_entry_ids.update(chunk_ids)
                raw = _call_llm_with_retries(
                    prompt,
                    model=llm_config["model"],
                    base_url=llm_config["base_url"],
                    api_key=llm_config["api_key"],
                    timeout=options.timeout,
                    api_mode=llm_config.get("api_mode", "chat_completions"),
                    endpoint=str(llm_config.get("endpoint") or ""),
                    append_v1=_config_bool(llm_config, "append_v1", True),
                    allow_insecure_endpoint=explicit_insecure_endpoint_opt_in(
                        llm_config.get("allow_insecure_endpoint")
                    ),
                    thinking=(
                        llm_config.get("thinking")
                        if isinstance(llm_config.get("thinking"), dict)
                        else None
                    ),
                    max_attempts=max_attempts,
                    retry_delay=retry_delay,
                )
                parsed_candidates, parser_status = _parse_journal_llm_candidates(
                    raw,
                    bundle=bundle,
                    scope_id=profile.scope_id,
                    shared_scope_id=profile.shared_scope_id,
                    allowed_target_ids=allowed_target_ids,
                    allowed_target_ids_by_scope=allowed_target_ids_by_scope,
                    allowed_message_ids=chunk_ids,
                )
                extractor_status_counts[parser_status] += 1
                if parser_status == "explicit_skip":
                    reviewed_entry_ids.update(chunk_ids)
                elif parser_status in {"empty", "filtered"}:
                    unresolved_entry_ids.update(chunk_ids)
                chunk_tool_ids = _exact_covered_tool_ids(
                    entries,
                    identity=bundle_identity,
                    chunk_ids=chunk_ids,
                )
                output.extend(
                    _journal_from_digest_candidate(
                        candidate, covered_tool_ids=chunk_tool_ids
                    )
                    for candidate in parsed_candidates
                )
            except Exception as exc:
                # Before the first real attempt, keep the zero-attempt
                # fail-closed raise. After any attempt, never bare-raise:
                # prefix candidates and exact attempted IDs must survive.
                if not attempted_entry_ids:
                    raise
                if not chunk_attempted:
                    continue
                normalized = _normalize_chunk_exception(exc)
                chunk_errors.append(normalized)
                extractor_status_counts[normalized.error_kind or "error"] += 1
                unresolved_entry_ids.update(chunk_ids)
                if normalized.retryable:
                    retryable_unresolved_entry_ids.update(chunk_ids)
                continue
        # Untouched bundle IDs — overflow or a later uncalled chunk — stay
        # deferred. Do not substitute the whole loaded window.
        deferred_entry_ids.update(bundle_entry_ids - covered_entry_ids)
        deferred_entry_ids.update(
            _uncovered_digestible_tool_ids(
                entries,
                identity=bundle_identity,
                covered_entry_ids=covered_entry_ids,
            )
        )
    extractor_error = None
    if chunk_errors and attempted_chunks == len(chunk_errors) and not output:
        # Keep exact attempted/deferred/reviewed sets. Raising here used to
        # make the digest treat every loaded row as retryable-failed.
        try:
            _reason, extractor_error = _quarantine_classification(chunk_errors[0])
        except Exception:
            extractor_error = _safe_extractor_error_meta()
    return JournalCandidateList(
        [candidate for candidate in output if candidate.entry_ids],
        extractor_status_counts=extractor_status_counts,
        reviewed_entry_ids=reviewed_entry_ids,
        unresolved_entry_ids=unresolved_entry_ids,
        retryable_unresolved_entry_ids=retryable_unresolved_entry_ids,
        deferred_entry_ids=deferred_entry_ids,
        attempted_entry_ids=attempted_entry_ids,
        extractor_error=extractor_error,
    )
