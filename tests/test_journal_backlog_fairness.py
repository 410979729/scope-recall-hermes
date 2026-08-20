"""Backlog fairness and bounded-retry regressions for issues #43/#45/#46/#47.

Before these fixes: budget-overflow entries and tool-only sessions reloaded on
every digest run forever, unresolved chunks retried without any cap, outbox
retention collided with live writers on every idle tick, and a same-process
peer's dirty transaction could fail provider startup outright.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import types
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

import scope_recall.journal as journal_module
import scope_recall.journal_extractors as journal_extractors_module
import scope_recall.vector_runtime as vector_runtime_module
from scope_recall.journal_llm import JournalDigestLLMError
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.journal import (
    append_journal_entry,
    ensure_journal_schema,
    run_journal_digest,
)
from scope_recall.journal_store import load_unprocessed_journal_entries
from scope_recall.sql_store import ensure_schema


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="9000000001",  # fixture
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _home(tmp_path: Path, journal_config: dict) -> tuple[Path, sqlite3.Connection]:
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}, "journal": journal_config}),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text(
        "SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8"
    )
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return hermes_home, conn


def _append(conn, scope, *, session: str, turn: int, content: str, role: str = "user") -> int:
    return append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id=session,
        turn_number=turn,
        role=role,
        content=content,
    )


def _journal_backlog_rows(conn) -> dict[int, sqlite3.Row]:
    return {
        int(row["id"]): row
        for row in conn.execute(
            "SELECT id, processed_run_id, processed_at, deferred_run_id, deferred_at, "
            "defer_count, extraction_attempts, retryable_failures FROM journal_entries"
        ).fetchall()
    }


def _deferred_entry_ids(rows: dict[int, sqlite3.Row], entry_ids: list[int]) -> list[int]:
    return [
        entry_id
        for entry_id in entry_ids
        if str(rows[entry_id]["deferred_run_id"] or "")
        and not str(rows[entry_id]["processed_run_id"] or "")
    ]


def _action_entry_ids(result: dict, action_name: str) -> set[int]:
    ids: set[int] = set()
    for action in result.get("actions") or []:
        if action.get("action") != action_name:
            continue
        for raw in action.get("entry_ids") or []:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                continue
    return ids


def _explicit_leave_sets(
    conn: sqlite3.Connection, result: dict, loaded_ids: set[int]
) -> tuple[set[int], set[int], set[int], set[int]]:
    """Derive the four explicit leave states from durable markers plus run actions."""

    rows = _journal_backlog_rows(conn)
    processed: set[int] = set()
    quarantined: set[int] = set()
    deferred: set[int] = set()
    for entry_id in loaded_ids:
        row = rows[entry_id]
        processed_run = str(row["processed_run_id"] or "")
        deferred_run = str(row["deferred_run_id"] or "")
        if processed_run:
            rejection = conn.execute(
                "SELECT reason FROM journal_rejections "
                "WHERE journal_entry_id = ? AND run_id = ?",
                (entry_id, processed_run),
            ).fetchone()
            reason = str(rejection["reason"] if rejection else "")
            if (
                "bounded attempts" in reason
                or reason.startswith("dead-letter:")
                or reason.startswith("retry-exhausted:")
            ):
                quarantined.add(entry_id)
            else:
                processed.add(entry_id)
        elif deferred_run:
            deferred.add(entry_id)
    pending = _action_entry_ids(result, "pending") & loaded_ids
    reported = result.get("leave_states") or {}
    if isinstance(reported, dict):
        pending |= {
            int(entry_id)
            for entry_id in reported.get("retryable_pending_ids") or []
            if int(entry_id) in loaded_ids
        }
    return processed, pending, deferred, quarantined


def _assert_exclusive_four_states(
    processed: set[int],
    pending: set[int],
    deferred: set[int],
    quarantined: set[int],
    loaded_ids: set[int],
) -> None:
    groups = (processed, pending, deferred, quarantined)
    assert loaded_ids == processed | pending | deferred | quarantined
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            assert not (left & right)


def _entry_state(conn, entry_ids):
    rows = _journal_backlog_rows(conn)
    return {entry_id: rows[entry_id] for entry_id in entry_ids}


def test_budget_deferred_entries_become_visible_and_keep_backlog_position(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
        },
    )
    scope = _scope()
    contents = [
        f"这是第 {index} 条足够长的工程讨论记录，涉及 scope-recall 的检索改造与写入路径细节说明。"
        for index in range(1, 6)
    ]
    entry_ids = [
        _append(conn, scope, session="fat-session", turn=index, content=text)
        for index, text in enumerate(contents, start=1)
    ]

    def empty_llm(prompt: str, **kwargs) -> str:
        return "[]"

    monkeypatch.setattr(journal_module, "call_llm", empty_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="test", limit_entries=50
    )
    assert result["ok"] is True

    rows = {
        int(row["id"]): row
        for row in conn.execute(
            "SELECT id, processed_run_id, deferred_run_id, extraction_attempts "
            "FROM journal_entries"
        ).fetchall()
    }
    deferred = [
        entry_id
        for entry_id in entry_ids
        if str(rows[entry_id]["deferred_run_id"] or "")
    ]
    attempted = [
        entry_id
        for entry_id in entry_ids
        if int(rows[entry_id]["extraction_attempts"] or 0) > 0
    ]
    # The session budget only covers a prefix; the rest must be visibly
    # deferred instead of silently reloading, and nothing is marked processed.
    assert deferred, "budget overflow must record deferred bookkeeping"
    assert attempted, "covered-but-unresolved entries must consume one attempt"
    assert not set(deferred) & set(attempted)
    assert all(not str(rows[entry_id]["processed_run_id"] or "") for entry_id in entry_ids)
    deferred_actions = [
        action for action in result.get("actions", []) if action.get("action") == "deferred"
    ]
    assert deferred_actions and deferred_actions[0]["entry_count"] == len(deferred)


def test_unresolved_entries_quarantine_after_bounded_attempts(tmp_path, monkeypatch):
    hermes_home, conn = _home(
        tmp_path,
        {"extractor": "llm", "extraction_attempts_quarantine": 2},
    )
    scope = _scope()
    entry_id = _append(
        conn,
        scope,
        session="stuck-session",
        turn=1,
        content="这条记录的内容会让抽取器每次都返回空结果，用来验证隔离阈值。",
    )

    monkeypatch.setattr(journal_module, "call_llm", lambda prompt, **kwargs: "[]")

    first = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t1", limit_entries=10
    )
    assert first["ok"] is True
    row = conn.execute(
        "SELECT extraction_attempts, processed_run_id FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert int(row["extraction_attempts"]) == 1
    assert not str(row["processed_run_id"] or "")

    second = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t2", limit_entries=10
    )
    assert second["ok"] is True
    row = conn.execute(
        "SELECT extraction_attempts, processed_run_id FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert int(row["extraction_attempts"]) == 2
    assert str(row["processed_run_id"] or ""), "quarantined entry must leave the backlog"
    rejection = conn.execute(
        "SELECT reason FROM journal_rejections WHERE journal_entry_id=?",
        (entry_id,),
    ).fetchone()
    assert rejection is not None
    assert "bounded attempts" in str(rejection["reason"])

    third = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t3", limit_entries=10
    )
    assert third.get("status") in {"ok", "no_unprocessed"} or third.get("ok") is True


def test_transient_llm_failures_do_not_consume_attempts(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path, {"extractor": "llm"})
    scope = _scope()
    entry_id = _append(
        conn,
        scope,
        session="flaky-session",
        turn=1,
        content="临时网络故障不应该消耗这条记录的有界抽取次数预算。",
    )

    def transient_failure(*args, **kwargs):
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=3, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(
        journal_extractors_module, "_call_llm_with_retries", transient_failure
    )

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t", limit_entries=10
    )
    # A fully-failed transient run keeps its failure receipt (ok=False); the
    # invariant under test is that the entry neither consumed a bounded
    # attempt nor left the backlog.
    assert result["ok"] is False
    row = conn.execute(
        "SELECT extraction_attempts, processed_run_id FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert int(row["extraction_attempts"]) == 0
    assert not str(row["processed_run_id"] or "")


def test_tool_only_sessions_leave_the_backlog_as_reviewed(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path, {"extractor": "llm"})
    scope = _scope()
    entry_id = _append(
        conn,
        scope,
        session="tool-only-session",
        turn=1,
        role="tool",
        content="tool execute_command exit=0 duration=1.2s summary: build completed",
    )

    def must_not_call(*args, **kwargs):
        raise AssertionError("tool-only sessions must not reach the LLM")

    monkeypatch.setattr(journal_module, "call_llm", must_not_call)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t", limit_entries=10
    )
    assert result["ok"] is True
    row = conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id=?", (entry_id,)
    ).fetchone()
    assert str(row["processed_run_id"] or ""), (
        "tool-only sessions must exit the backlog instead of reloading forever"
    )


def test_loader_per_session_cap_round_robins_sessions(tmp_path):
    _, conn = _home(tmp_path, {})
    scope = _scope()
    for turn in range(1, 6):
        _append(conn, scope, session="fat", turn=turn, content=f"fat session entry {turn} with enough length to pass capture filters easily")
    for turn in range(1, 3):
        _append(conn, scope, session="thin", turn=turn, content=f"thin session entry {turn} with enough length to pass capture filters easily")

    entries = load_unprocessed_journal_entries(
        conn,
        scope_ids=[build_scope_id(scope)],
        limit=4,
        per_session_limit=2,
    )
    sessions = [entry.session_id for entry in entries]
    assert sessions.count("fat") == 2
    assert sessions.count("thin") == 2


def test_oversized_session_cap_clamps_below_run_limit():
    from scope_recall.journal_store import effective_per_session_limit

    assert effective_per_session_limit(200, 80, 10) == 10
    assert effective_per_session_limit(200, 8, 2) == 4
    assert effective_per_session_limit(10, 80, 10) == 10
    assert effective_per_session_limit(None, 80, 1) == 80
    assert effective_per_session_limit(200, 80, 1) == 80


def test_clamped_session_cap_lets_new_session_into_small_window(tmp_path):
    from scope_recall.journal_store import (
        effective_per_session_limit,
        load_unprocessed_journal_entries,
    )

    _, conn = _home(tmp_path, {})
    scope = _scope()
    for turn in range(1, 21):
        _append(
            conn,
            scope,
            session="fat-old",
            turn=turn,
            content=f"old fat backlog {turn} with enough length to stay durable",
        )
    _append(
        conn,
        scope,
        session="brand-new",
        turn=1,
        content="new session line with enough length to stay durable in journal",
    )
    cap = effective_per_session_limit(200, 8, 2)
    entries = load_unprocessed_journal_entries(
        conn,
        scope_ids=[build_scope_id(scope)],
        limit=8,
        per_session_limit=cap,
    )
    sessions = {entry.session_id for entry in entries}
    assert "brand-new" in sessions


def _retention_provider() -> types.SimpleNamespace:
    return types.SimpleNamespace(_lock=threading.RLock())


def test_outbox_retention_is_rate_limited(monkeypatch):
    provider = _retention_provider()
    assert vector_runtime_module._outbox_retention_due(provider, interval_seconds=900)
    assert not vector_runtime_module._outbox_retention_due(provider, interval_seconds=900)


def test_outbox_retention_skips_quietly_under_contention(monkeypatch, caplog):
    provider = _retention_provider()
    conn = sqlite3.connect(":memory:")

    def locked_prune(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        vector_runtime_module, "prune_completed_vector_outbox", locked_prune
    )

    import logging

    with caplog.at_level(logging.DEBUG, logger=vector_runtime_module.logger.name):
        for expected_skips in range(1, 8):
            receipt = vector_runtime_module._prune_completed_outbox(
                provider, conn, retention_days=30, keep_per_generation=5000
            )
            assert receipt["status"] == "skipped_contention"
            assert receipt["consecutive_skips"] == expected_skips
        warnings = [
            record for record in caplog.records if record.levelname == "WARNING"
        ]
        assert not warnings, "sub-threshold contention skips must not warn"

        receipt = vector_runtime_module._prune_completed_outbox(
            provider, conn, retention_days=30, keep_per_generation=5000
        )
        assert receipt["consecutive_skips"] == 8
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1, "the escalation threshold must warn exactly once"


def test_outbox_retention_success_resets_contention_counter(monkeypatch):
    provider = _retention_provider()
    provider._outbox_retention_contention_skips = 5
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        vector_runtime_module,
        "prune_completed_vector_outbox",
        lambda *args, **kwargs: {"deleted": 0},
    )
    receipt = vector_runtime_module._prune_completed_outbox(
        provider, conn, retention_days=30, keep_per_generation=5000
    )
    assert receipt["status"] == "unchanged"
    assert provider._outbox_retention_contention_skips == 0


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_initialize_recovers_after_peer_rollback(tmp_path, monkeypatch):
    """Issue #43: startup retries once when peer rollback frees the lock."""

    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = load_memory_provider("scope-recall")
    assert provider is not None

    attempts = {"count": 0}
    real_open = provider._open_runtime_connection

    def flaky_open():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_open()

    rollbacks = {"count": 0}

    def fake_peer_rollback(context: str):
        rollbacks["count"] += 1
        return {"peer_rollbacks": 1, "peer_providers_checked": 1}

    monkeypatch.setattr(provider, "_open_runtime_connection", flaky_open)
    monkeypatch.setattr(
        provider, "_rollback_peer_provider_transactions", fake_peer_rollback
    )
    try:
        provider.initialize(
            "peer-recovery-session",
            hermes_home=str(tmp_path),
            platform="cli",
            user_id="peer-user",
            chat_id="peer-chat",
            agent_identity="tester",
            agent_workspace="hermes",
            agent_context="primary",
        )
        assert provider.runtime_status == "active"
        assert attempts["count"] == 2
        assert rollbacks["count"] == 1
    finally:
        provider.shutdown()


def test_initialize_raises_when_no_peer_rollback_helps(tmp_path, monkeypatch):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = load_memory_provider("scope-recall")
    assert provider is not None

    monkeypatch.setattr(
        provider,
        "_open_runtime_connection",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    monkeypatch.setattr(
        provider,
        "_rollback_peer_provider_transactions",
        lambda context: {"peer_rollbacks": 0, "peer_providers_checked": 0},
    )
    with pytest.raises(sqlite3.OperationalError):
        provider.initialize(
            "peer-recovery-fails",
            hermes_home=str(tmp_path),
            platform="cli",
            user_id="peer-user",
            chat_id="peer-chat",
            agent_identity="tester",
            agent_workspace="hermes",
            agent_context="primary",
        )
    # The writer lease must not stay stranded after the failed startup.
    provider2 = load_memory_provider("scope-recall")
    try:
        provider2.initialize(
            "peer-recovery-after-failure",
            hermes_home=str(tmp_path),
            platform="cli",
            user_id="peer-user",
            chat_id="peer-chat",
            agent_identity="tester",
            agent_workspace="hermes",
            agent_context="primary",
        )
        assert provider2.runtime_status == "active"
        assert provider2._truth_writer_role == "owner"
    finally:
        provider2.shutdown()

def test_second_digest_run_advances_previously_deferred_session_prefix(
    tmp_path, monkeypatch
):
    """Issue #46: deferred markers must resume a later prefix, not reload the same one."""

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
            "extraction_attempts_quarantine": 99,
        },
    )
    scope = _scope()
    contents = [
        f"这是第 {index} 条足够长的工程讨论记录，涉及 scope-recall 的检索改造与写入路径细节说明。"
        for index in range(1, 6)
    ]
    entry_ids = [
        _append(conn, scope, session="fat-session", turn=index, content=text)
        for index, text in enumerate(contents, start=1)
    ]

    monkeypatch.setattr(journal_module, "call_llm", lambda prompt, **kwargs: "[]")

    first = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t1", limit_entries=50
    )
    assert first["ok"] is True
    first_deferred = _deferred_entry_ids(_journal_backlog_rows(conn), entry_ids)
    assert first_deferred, "first run must overflow a suffix so resume has work"
    first_loaded = set(first.get("leave_states", {}).get("deferred_ids") or []) | set(
        first.get("leave_states", {}).get("retryable_pending_ids") or []
    ) | set(first.get("leave_states", {}).get("processed_ids") or []) | set(
        first.get("leave_states", {}).get("quarantined_ids") or []
    )
    if first_loaded:
        _assert_exclusive_four_states(
            *_explicit_leave_sets(conn, first, first_loaded),
            first_loaded,
        )

    second = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t2", limit_entries=50
    )
    assert second["ok"] is True
    rows = _journal_backlog_rows(conn)
    second_deferred = _deferred_entry_ids(rows, entry_ids)
    advanced = [
        entry_id
        for entry_id in first_deferred
        if int(rows[entry_id]["extraction_attempts"] or 0) > 0
        or str(rows[entry_id]["processed_run_id"] or "")
        or entry_id not in second_deferred
    ]
    assert advanced, (
        "a previously deferred row must be attempted, processed, or otherwise "
        "resumed instead of reloading the same prefix"
    )
    assert set(second_deferred) != set(first_deferred)
    second_loaded = set(second.get("leave_states", {}).get("deferred_ids") or []) | set(
        second.get("leave_states", {}).get("retryable_pending_ids") or []
    ) | set(second.get("leave_states", {}).get("processed_ids") or []) | set(
        second.get("leave_states", {}).get("quarantined_ids") or []
    )
    if second_loaded:
        _assert_exclusive_four_states(
            *_explicit_leave_sets(conn, second, second_loaded),
            second_loaded,
        )


def test_second_run_clears_current_deferral_for_retryable_provider_pending(
    tmp_path, monkeypatch
):
    """A later covered row that stays retryable-pending must not keep current deferral."""

    from scope_recall.doctor_journal import journal_report
    from scope_recall.journal_extractors import JournalCandidateList
    from scope_recall.journal_store import load_session_digest_state
    from scope_recall.scope import build_scope_id

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
            "extraction_attempts_quarantine": 99,
        },
    )
    scope = _scope()
    contents = [
        f"这是第 {index} 条足够长的工程讨论记录，涉及 scope-recall 的检索改造与写入路径细节说明。"
        for index in range(1, 6)
    ]
    entry_ids = [
        _append(conn, scope, session="fat-session", turn=index, content=text)
        for index, text in enumerate(contents, start=1)
    ]
    monkeypatch.setattr(journal_module, "call_llm", lambda prompt, **kwargs: "[]")
    first = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t1", limit_entries=50
    )
    assert first["ok"] is True
    first_deferred = _deferred_entry_ids(_journal_backlog_rows(conn), entry_ids)
    assert first_deferred
    first_defer_counts = {
        entry_id: int(_journal_backlog_rows(conn)[entry_id]["defer_count"] or 0)
        for entry_id in first_deferred
    }
    target_id = first_deferred[0]

    def retryable_cover(conn_inner, *, entries, hermes_home, scope, journal_config):
        del conn_inner, hermes_home, scope, journal_config
        loaded = [int(entry.id) for entry in entries]
        return JournalCandidateList(
            [],
            unresolved_entry_ids={target_id} & set(loaded),
            retryable_unresolved_entry_ids={target_id} & set(loaded),
            deferred_entry_ids=set(loaded) - {target_id},
        )

    monkeypatch.setattr(journal_module, "llm_journal_candidates", retryable_cover)
    second = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t2", limit_entries=50
    )
    assert second["ok"] is True
    row = _journal_backlog_rows(conn)[target_id]
    assert str(row["deferred_run_id"] or "") == ""
    assert row["deferred_at"] is None
    assert int(row["defer_count"] or 0) == first_defer_counts[target_id]
    assert not str(row["processed_run_id"] or "")
    processed, pending, deferred, quarantined = _explicit_leave_sets(
        conn, second, {target_id}
    )
    assert pending == {target_id}
    assert target_id not in deferred
    _assert_exclusive_four_states(processed, pending, deferred, quarantined, {target_id})

    payload, check, _recommendations = journal_report(hermes_home)
    assert check["ok"] is True
    current_deferred = [
        entry_id
        for entry_id in entry_ids
        if str(_journal_backlog_rows(conn)[entry_id]["deferred_run_id"] or "")
        and not str(_journal_backlog_rows(conn)[entry_id]["processed_run_id"] or "")
    ]
    assert payload["backlog"]["deferred"]["count"] == len(current_deferred)
    assert target_id not in current_deferred
    cursor = load_session_digest_state(
        conn, scope_id=build_scope_id(scope), session_id="fat-session"
    )
    assert cursor is not None


def test_second_run_clears_current_deferral_for_parsed_uncited_pending(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
            "extraction_attempts_quarantine": 99,
        },
    )
    scope = _scope()
    contents = [
        f"这是第 {index} 条足够长的工程讨论记录，涉及 scope-recall 的检索改造与写入路径细节说明。"
        for index in range(1, 6)
    ]
    entry_ids = [
        _append(conn, scope, session="uncited-session", turn=index, content=text)
        for index, text in enumerate(contents, start=1)
    ]
    monkeypatch.setattr(journal_module, "call_llm", lambda prompt, **kwargs: "[]")
    first = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="u1", limit_entries=50
    )
    assert first["ok"] is True
    first_deferred = _deferred_entry_ids(_journal_backlog_rows(conn), entry_ids)
    assert len(first_deferred) >= 2
    uncited_id = first_deferred[0]
    cited_id = first_deferred[1]
    prior_defer_count = int(_journal_backlog_rows(conn)[uncited_id]["defer_count"] or 0)

    def subset_llm(prompt: str, **kwargs):
        del prompt, kwargs
        return json.dumps(
            [
                {
                    "action": "insert",
                    "evidence_message_ids": [cited_id],
                    "content": (
                        "scope-recall journal digest must clear current deferral "
                        "when a formerly deferred row is parsed but uncited."
                    ),
                    "target": "memory",
                    "memory_type": "procedure",
                    "importance": 0.9,
                    "confidence": 0.86,
                    "entities": ["scope-recall", "journal digest"],
                    "tags": ["leave-state", "uncited"],
                    "reason": "LLM cited a sibling in the resumed chunk.",
                }
            ]
        )

    monkeypatch.setattr(journal_module, "call_llm", subset_llm)
    second = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="u2", limit_entries=50
    )
    assert second["ok"] is True
    row = _journal_backlog_rows(conn)[uncited_id]
    assert str(row["deferred_run_id"] or "") == ""
    assert row["deferred_at"] is None
    assert int(row["defer_count"] or 0) == prior_defer_count
    processed, pending, deferred, quarantined = _explicit_leave_sets(
        conn, second, {uncited_id}
    )
    assert pending == {uncited_id}
    _assert_exclusive_four_states(processed, pending, deferred, quarantined, {uncited_id})


def test_digest_dry_run_does_not_mutate_cursor_or_deferral(tmp_path, monkeypatch):
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
        },
    )
    scope = _scope()
    from scope_recall.journal_store import load_session_digest_state
    from scope_recall.scope import build_scope_id

    for index in range(1, 6):
        _append(
            conn,
            scope,
            session="dry-session",
            turn=index,
            content=(
                f"这是第 {index} 条足够长的工程讨论记录，"
                "涉及 scope-recall 的检索改造与写入路径细节说明。"
            ),
        )
    monkeypatch.setattr(journal_module, "call_llm", lambda prompt, **kwargs: "[]")
    result = run_journal_digest(
        hermes_home=hermes_home,
        scope=scope,
        interval_label="dry",
        limit_entries=50,
        dry_run=True,
    )
    assert result.get("status") == "dry_run" or result["ok"] is True
    rows = _journal_backlog_rows(conn)
    assert all(not str(row["deferred_run_id"] or "") for row in rows.values())
    assert all(int(row["defer_count"] or 0) == 0 for row in rows.values())
    assert all(not str(row["processed_run_id"] or "") for row in rows.values())
    assert (
        load_session_digest_state(
            conn, scope_id=build_scope_id(scope), session_id="dry-session"
        )
        is None
    )


def test_digest_leave_and_cursor_writes_roll_back_on_late_failure(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
        },
    )
    scope = _scope()
    from scope_recall.journal_store import load_session_digest_state
    from scope_recall.scope import build_scope_id

    for index in range(1, 6):
        _append(
            conn,
            scope,
            session="rollback-session",
            turn=index,
            content=(
                f"这是第 {index} 条足够长的工程讨论记录，"
                "涉及 scope-recall 的检索改造与写入路径细节说明。"
            ),
        )
    monkeypatch.setattr(journal_module, "call_llm", lambda prompt, **kwargs: "[]")

    def boom(*args, **kwargs):
        raise RuntimeError("injected late digest failure")

    monkeypatch.setattr(journal_module, "journal_digest_receipt_fields", boom)
    with pytest.raises(RuntimeError, match="injected late digest failure"):
        run_journal_digest(
            hermes_home=hermes_home, scope=scope, interval_label="boom", limit_entries=50
        )
    rows = _journal_backlog_rows(conn)
    assert all(not str(row["deferred_run_id"] or "") for row in rows.values())
    assert all(int(row["defer_count"] or 0) == 0 for row in rows.values())
    assert all(int(row["extraction_attempts"] or 0) == 0 for row in rows.values())
    assert all(not str(row["processed_run_id"] or "") for row in rows.values())
    assert (
        load_session_digest_state(
            conn, scope_id=build_scope_id(scope), session_id="rollback-session"
        )
        is None
    )


def test_parsed_subset_assigns_every_loaded_id_an_exclusive_leave_state(
    tmp_path, monkeypatch
):
    """Issue #46: a parsed candidate that cites a subset cannot leave leftovers ghosted."""

    hermes_home, conn = _home(tmp_path, {"extractor": "llm"})
    scope = _scope()
    entry_ids = [
        _append(
            conn,
            scope,
            session="subset-session",
            turn=index,
            content=(
                f"Joy 要求第 {index} 条 journal 记录进入同一抽取片段，"
                "用来验证 cited subset 之后未引用条目也必须离开明确状态。"
            ),
        )
        for index in range(1, 3)
    ]
    cited_id = entry_ids[0]
    loaded_ids = set(entry_ids)

    def subset_llm(prompt: str, **kwargs) -> str:
        del prompt, kwargs
        return json.dumps(
            [
                {
                    "action": "insert",
                    "evidence_message_ids": [cited_id],
                    "content": (
                        "scope-recall journal digest must close every loaded "
                        "entry after a successful parsed subset citation."
                    ),
                    "target": "memory",
                    "memory_type": "procedure",
                    "importance": 0.9,
                    "confidence": 0.86,
                    "entities": ["scope-recall", "journal digest"],
                    "tags": ["leave-state", "subset"],
                    "reason": "LLM cited only the first message in a two-entry chunk.",
                }
            ]
        )

    monkeypatch.setattr(journal_module, "call_llm", subset_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="subset", limit_entries=10
    )
    assert result["ok"] is True

    processed_ids, pending_ids, deferred_ids, quarantined_ids = _explicit_leave_sets(
        conn, result, loaded_ids
    )
    assert cited_id in processed_ids
    _assert_exclusive_four_states(
        processed_ids, pending_ids, deferred_ids, quarantined_ids, loaded_ids
    )

def test_persistent_retryable_llm_failure_is_bounded_and_unblocks_same_session(
    tmp_path, monkeypatch
):
    """Persistent retryable timeouts must not pin the same-session FIFO head.

    One transient failure still leaves the row pending and must not burn the
    ordinary extraction-quality budget. After a small durable cross-run bound,
    the old row has to leave that FIFO head so a newer same-session row can
    be selected. Exit is a replayable rejection, not silent drop.
    """

    bound = 2
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "retryable_failures_quarantine": bound,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
        },
    )
    scope = _scope()
    old_id = _append(
        conn,
        scope,
        session="starved-session",
        turn=1,
        content="旧行持续 timeout 时不得永远占住同一 session 的 FIFO 队头。",
    )

    calls = {"count": 0}

    def persistent_timeout(*args, **kwargs):
        calls["count"] += 1
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(
        journal_extractors_module, "_call_llm_with_retries", persistent_timeout
    )

    first = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="t1", limit_entries=1
    )
    assert first["ok"] is False
    old = conn.execute(
        "SELECT extraction_attempts, processed_run_id FROM journal_entries WHERE id=?",
        (old_id,),
    ).fetchone()
    assert int(old["extraction_attempts"] or 0) == 0
    assert not str(old["processed_run_id"] or ""), (
        "one transient retryable failure must stay pending"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM journal_rejections WHERE journal_entry_id=?",
        (old_id,),
    ).fetchone()[0] == 0

    new_id = _append(
        conn,
        scope,
        session="starved-session",
        turn=2,
        content="同一 session 的新行必须在旧行耗尽可重试预算后变成可选项。",
    )

    for label in ("t2", "t3", "t4"):
        run_journal_digest(
            hermes_home=hermes_home, scope=scope, interval_label=label, limit_entries=1
        )

    old = conn.execute(
        "SELECT extraction_attempts, processed_run_id, retryable_failures "
        "FROM journal_entries WHERE id=?",
        (old_id,),
    ).fetchone()
    new = conn.execute(
        "SELECT processed_run_id FROM journal_entries WHERE id=?",
        (new_id,),
    ).fetchone()
    assert int(old["extraction_attempts"] or 0) == 0, (
        "retryable timeouts must not spend the ordinary extraction-quality budget"
    )
    assert int(old["retryable_failures"] or 0) >= bound
    assert str(old["processed_run_id"] or ""), (
        "persistent retryable failure must leave the same-session FIFO head"
    )
    rejection = conn.execute(
        "SELECT reason, candidate FROM journal_rejections WHERE journal_entry_id=?",
        (old_id,),
    ).fetchone()
    assert rejection is not None, "exit must be replayable on the rejection ledger"
    assert str(rejection["reason"]).startswith("retry-exhausted:")
    assert "旧行持续 timeout" not in str(rejection["candidate"] or "")

    head = load_unprocessed_journal_entries(
        conn,
        scope_ids=[build_scope_id(scope)],
        limit=1,
    )
    if head:
        assert head[0].id == new_id, (
            "newer same-session row must become the selectable FIFO head"
        )
    else:
        assert str(new["processed_run_id"] or ""), (
            "newer same-session row must have become selectable after the old head exited"
        )
    assert calls["count"] >= bound


def test_all_chunk_retryable_failure_attributes_only_attempted_ids(
    tmp_path, monkeypatch
):
    """All-timeout runs must not increment or evict unattempted loaded rows.

    A single-row timeout cannot catch the R1 defect: ``llm_journal_candidates``
    raises and ``run_journal_digest`` then treats every loaded id as retryable,
    including budget-deferred suffix rows and evidence-only admission rows.
    """

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
            "extraction_attempts_quarantine": 9,
            "retryable_failures_quarantine": 9,
        },
    )
    scope = _scope()
    contents = [
        f"ATTEMPTED-SECRET-BODY-7e8d 这是第 {index} 条足够长的工程讨论记录，涉及 scope-recall 的检索改造与写入路径细节说明。"
        for index in range(1, 4)
    ]
    contents.extend(
        [
            f"DEFERRED-SECRET-BODY-9f3c 这是第 {index} 条足够长的工程讨论记录，预算耗尽后不得被算成可重试失败。"
            for index in range(4, 6)
        ]
    )
    digestible_ids = [
        _append(conn, scope, session="attr-session", turn=index, content=text)
        for index, text in enumerate(contents, start=1)
    ]
    evidence_id = _append(
        conn,
        scope,
        session="attr-session",
        turn=6,
        role="tool",
        content=(
            "EVIDENCE-SECRET-BODY-2a1b tool execution trace with a large diff "
            "that should stay on the admission path."
        ),
    )
    loaded_ids = [*digestible_ids, evidence_id]

    attempted_from_calls: set[int] = set()
    increment_ids: list[int] = []

    def transient_timeout(prompt: str, **kwargs):
        del kwargs
        attempted_from_calls.update(
            int(match) for match in re.findall(r"message_id=(\d+)", prompt)
        )
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    real_increment = journal_module.increment_retryable_failures

    def counting_increment(conn, *, entry_ids, commit=True):
        increment_ids.extend(int(entry_id) for entry_id in entry_ids)
        return real_increment(conn, entry_ids=entry_ids, commit=commit)

    monkeypatch.setattr(
        journal_extractors_module, "_call_llm_with_retries", transient_timeout
    )
    monkeypatch.setattr(
        journal_module, "increment_retryable_failures", counting_increment
    )

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="attr", limit_entries=50
    )
    rows = _entry_state(conn, loaded_ids)
    unattempted_ids = [
        entry_id for entry_id in digestible_ids if entry_id not in attempted_from_calls
    ]
    evidence = rows[evidence_id]
    serialized = json.dumps(result, ensure_ascii=False)

    assert attempted_from_calls, "at least one digestible prefix must reach the LLM"
    assert unattempted_ids, (
        "tiny per-session budget must leave a digestible suffix unattempted"
    )
    assert evidence_id not in attempted_from_calls

    assert result["ok"] is False
    assert result.get("status") == "error"
    assert result.get("extractor_used") == "llm-error"
    assert "timeout" in str(result.get("error") or "")
    assert "ATTEMPTED-SECRET-BODY-7e8d" not in serialized
    assert "DEFERRED-SECRET-BODY-9f3c" not in serialized
    assert "EVIDENCE-SECRET-BODY-2a1b" not in serialized

    for entry_id in attempted_from_calls:
        assert int(rows[entry_id]["retryable_failures"] or 0) == 1
        assert int(rows[entry_id]["extraction_attempts"] or 0) == 0
        assert not str(rows[entry_id]["processed_run_id"] or "")
    for entry_id in unattempted_ids:
        assert int(rows[entry_id]["retryable_failures"] or 0) == 0
        assert int(rows[entry_id]["extraction_attempts"] or 0) == 0
        assert not str(rows[entry_id]["processed_run_id"] or "")
        assert str(rows[entry_id]["deferred_run_id"] or ""), (
            "unattempted digestible suffix must keep visible budget-deferral"
        )
    assert int(evidence["retryable_failures"] or 0) == 0
    assert int(evidence["extraction_attempts"] or 0) == 0
    assert str(evidence["processed_run_id"] or ""), (
        "evidence-only rows keep the ordinary admission leave"
    )
    rejection = conn.execute(
        "SELECT reason FROM journal_rejections WHERE journal_entry_id=?",
        (evidence_id,),
    ).fetchone()
    assert rejection is not None
    assert str(rejection["reason"]) == "admission:tool_noise"

    assert increment_ids.count(evidence_id) == 0
    for entry_id in unattempted_ids:
        assert increment_ids.count(entry_id) == 0
    for entry_id in attempted_from_calls:
        assert increment_ids.count(entry_id) == 1
    assert int(result.get("retryable_failures") or 0) == len(attempted_from_calls)


def test_retryable_timeout_still_honors_explicit_heuristic_fallback(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": True,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
        },
    )
    scope = _scope()
    entry_id = _append(
        conn,
        scope,
        session="fallback-session",
        turn=1,
        content="显式开启 heuristic fallback 时，全超时不得丢掉这条可抽取的工程讨论。",
    )

    def transient_timeout(*args, **kwargs):
        del args, kwargs
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(
        journal_extractors_module, "_call_llm_with_retries", transient_timeout
    )

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="fallback", limit_entries=10
    )
    row = conn.execute(
        "SELECT extraction_attempts, retryable_failures, processed_run_id "
        "FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert result.get("extractor_used") == "heuristic-fallback"
    assert result.get("ok") is True
    assert int(row["retryable_failures"] or 0) == 0
    assert int(row["extraction_attempts"] or 0) == 0


def test_retryable_failures_are_consecutive_attempted_failures_not_lifetime(
    tmp_path, monkeypatch
):
    """A later non-retryable attempt must clear the active retryable budget.

    Timeout → deterministic unresolved → timeout must finish at 1, not 2.
    A merely loaded/deferred sibling must not be reset or incremented.
    """

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_chunk_chars": 220,
            "llm_max_session_chars": 260,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
            "extraction_attempts_quarantine": 9,
            "retryable_failures_quarantine": 9,
        },
    )
    scope = _scope()
    prefix_id = _append(
        conn,
        scope,
        session="consec-session",
        turn=1,
        content="CONSEC-ATTEMPTED 这条前缀会被真正送进抽取器，用来验证连续可重试失败预算。",
    )
    other_session_id = _append(
        conn,
        scope,
        session="consec-other-session",
        turn=1,
        content="另一 session 的行不得被这次连续预算路径重置或累计。",
    )
    mode = {"value": "timeout"}
    attempted_from_calls: set[int] = set()

    def scripted_llm(prompt: str, **kwargs):
        del kwargs
        attempted_from_calls.update(
            int(match) for match in re.findall(r"message_id=(\d+)", prompt)
        )
        if mode["value"] == "timeout":
            raise JournalDigestLLMError(
                "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
            )
        return "[]"

    monkeypatch.setattr(journal_extractors_module, "_call_llm_with_retries", scripted_llm)

    first = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="c1", limit_entries=1
    )
    assert first["ok"] is False
    rows = _entry_state(conn, [prefix_id, other_session_id])
    assert prefix_id in attempted_from_calls
    assert other_session_id not in attempted_from_calls
    assert int(rows[prefix_id]["retryable_failures"] or 0) == 1
    conn.execute(
        "UPDATE journal_entries SET retryable_failures=5 WHERE id=?",
        (other_session_id,),
    )
    conn.commit()

    mode["value"] = "empty"
    second = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="c2", limit_entries=1
    )
    assert second["ok"] is True
    rows = _entry_state(conn, [prefix_id, other_session_id])
    assert int(rows[prefix_id]["retryable_failures"] or 0) == 0
    assert int(rows[prefix_id]["extraction_attempts"] or 0) == 1
    assert not str(rows[prefix_id]["processed_run_id"] or "")
    assert int(rows[other_session_id]["retryable_failures"] or 0) == 5

    mode["value"] = "timeout"
    third = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="c3", limit_entries=1
    )
    assert third["ok"] is False
    rows = _entry_state(conn, [prefix_id, other_session_id])
    assert int(rows[prefix_id]["retryable_failures"] or 0) == 1
    assert int(rows[prefix_id]["extraction_attempts"] or 0) == 1
    assert int(rows[other_session_id]["retryable_failures"] or 0) == 5
