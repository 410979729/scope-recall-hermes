from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_runtime_config
from .gating import compact_text
from .journal_candidates import JournalDigestCandidate, _unique
from .journal_llm import JournalDigestLLMError, _call_llm_with_retries
from .journal_store import JournalEntry, _journal_entry_for_digest
from .models import RuntimeScope
from .nightly_digest import (
    DigestOptions,
    MessageRecord,
    ScopeProfile,
    SessionBundle,
    build_prompt,
    existing_memory_context,
    _parse_llm_candidates_with_status,
    resolve_llm_config,
    session_chunks,
)
from .scope import accessible_scope_ids, build_scope_id, build_shared_scope_id, normalize_scope_identity

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


def _parse_entry_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _journal_session_bundles(entries: list[JournalEntry]) -> list[SessionBundle]:
    grouped: dict[str, list[JournalEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.session_id or "unknown", []).append(entry)
    bundles: list[SessionBundle] = []
    for session_id, session_entries in grouped.items():
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
            )
        )
    return bundles


def _journal_from_digest_candidate(candidate: Any) -> JournalDigestCandidate:
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
    )


def _parse_journal_llm_candidates(raw: str, *, bundle: SessionBundle) -> list[Any]:
    candidates, status = _parse_llm_candidates_with_status(raw, bundle=bundle)
    if status == "parsed":
        return candidates
    if status in {"empty", "explicit_skip"}:
        return []
    error_kind = "parse" if status == "parse" else "filtered"
    raise JournalDigestLLMError(
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
    return bool(value)


def llm_journal_candidates(
    conn: sqlite3.Connection,
    *,
    entries: list[JournalEntry],
    hermes_home: Path,
    scope: RuntimeScope,
    journal_config: dict[str, Any],
) -> list[JournalDigestCandidate]:
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
    output: list[JournalDigestCandidate] = []
    max_attempts = _coerce_positive_int(journal_config.get("llm_max_attempts") or journal_config.get("llm_retry_attempts"), 3)
    retry_delay = _coerce_nonnegative_float(journal_config.get("llm_retry_delay"), 1.0)
    for bundle in _journal_session_bundles(entries):
        if bundle.source == "journal-tool-only":
            continue
        bundle_candidates: list[Any] = []
        for chunk in session_chunks(bundle, chunk_chars=options.chunk_chars, max_session_chars=options.max_session_chars):
            prompt = build_prompt(bundle, chunk, existing)
            raw = _call_llm_with_retries(
                prompt,
                model=llm_config["model"],
                base_url=llm_config["base_url"],
                api_key=llm_config["api_key"],
                timeout=options.timeout,
                api_mode=llm_config.get("api_mode", "chat_completions"),
                endpoint=str(llm_config.get("endpoint") or ""),
                append_v1=bool(llm_config.get("append_v1", True)),
                max_attempts=max_attempts,
                retry_delay=retry_delay,
            )
            bundle_candidates.extend(_parse_journal_llm_candidates(raw, bundle=bundle))
        output.extend(_journal_from_digest_candidate(candidate) for candidate in bundle_candidates)
    return [candidate for candidate in output if candidate.entry_ids]
