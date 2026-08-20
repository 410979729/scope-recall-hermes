"""P0-B / issue #47: digest must not hold a truth write transaction across LLM calls.

A deferred or write transaction left open across multi-minute extraction is
the 13-23 minute lock-holder class. Snapshot/plan reads must finish and
release before any network callback; apply uses a later short transaction.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

import scope_recall.journal as journal_module
import scope_recall.journal_extractors as journal_extractors
from datetime import date

import scope_recall.nightly_digest as nightly_digest
from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)
from scope_recall.journal import (
    JournalDigestCandidate,
    append_journal_entry,
    apply_journal_candidates,
    run_journal_digest,
)
from scope_recall.journal_extractors import llm_journal_candidates
from scope_recall.journal_store import load_unprocessed_journal_entries
from scope_recall.models import RuntimeScope
from scope_recall.nightly_digest import DigestOptions, MessageRecord, SessionBundle
from scope_recall.scope import accessible_scope_ids, build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema
from scope_recall.journal import ensure_journal_schema


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="digest-boundary-user",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _open_memory_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return conn


def _candidate_payload(entry_id: int, label: str) -> str:
    return json.dumps(
        [
            {
                "action": "insert",
                "evidence_message_ids": [entry_id],
                "content": (
                    f"scope-recall digest {label} must persist as a durable "
                    "candidate after the network callback returns."
                ),
                "target": "memory",
                "memory_type": "procedure",
                "importance": 0.9,
                "confidence": 0.86,
                "entities": ["scope-recall", label],
                "tags": ["digest-boundary", label],
                "reason": "LLM extracted a reusable procedure.",
            }
        ]
    )


def test_journal_digest_llm_callbacks_release_truth_transaction(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "journal": {
                    "extractor": "llm",
                    "llm_chunk_chars": 80,
                    "llm_max_session_chars": 4000,
                    "allow_heuristic_fallback": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text(
        "SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8"
    )
    db_path = storage / "memory.sqlite3"
    conn = _open_memory_db(db_path)
    scope = _scope()
    first_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="txn-boundary",
        turn_number=1,
        role="user",
        content=(
            "Joy 要求第一段 journal digest 在外部 LLM 回调期间不得持有 SQLite "
            "写事务，并且对端连接必须能 BEGIN IMMEDIATE。"
        ),
    )
    second_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="txn-boundary",
        turn_number=2,
        role="user",
        content=(
            "第二段候选必须单独走完网络回调；第一段 DML 不能把第二段 LLM "
            "藏进同一个长事务里，否则会重现 13 到 23 分钟锁持有。"
        ),
    )
    entries = load_unprocessed_journal_entries(
        conn, scope_ids=accessible_scope_ids(scope), limit=50
    )
    # Simulate the lock-holder class: a prior DML left this connection in a
    # write transaction before extraction starts the first network call.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS digest_boundary_probe(label TEXT PRIMARY KEY)"
    )
    conn.execute("INSERT OR IGNORE INTO digest_boundary_probe(label) VALUES ('held')")
    assert conn.in_transaction is True

    seen: list[dict[str, object]] = []
    call_index = {"n": 0}

    def fake_call_llm(prompt: str, **kwargs):
        del kwargs
        call_index["n"] += 1
        in_txn = bool(conn.in_transaction)
        peer = sqlite3.connect(db_path, timeout=0.2)
        try:
            peer.execute("BEGIN IMMEDIATE")
            peer.rollback()
            peer_ok = True
            peer_error = ""
        except sqlite3.OperationalError as exc:
            peer_ok = False
            peer_error = str(exc)
        finally:
            peer.close()
        seen.append(
            {
                "in_transaction": in_txn,
                "peer_begin_immediate": peer_ok,
                "peer_error": peer_error,
            }
        )
        entry_id = second_id if call_index["n"] >= 2 else first_id
        return _candidate_payload(entry_id, f"chunk-{call_index['n']}")

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        journal_extractors,
        "_call_llm_with_retries",
        journal_module._call_llm_with_retries,
    )

    candidates = llm_journal_candidates(
        conn,
        entries=entries,
        hermes_home=hermes_home,
        scope=scope,
        journal_config={
            "extractor": "llm",
            "llm_chunk_chars": 80,
            "llm_max_session_chars": 4000,
            "allow_heuristic_fallback": False,
            "api_key": "test-key",
        },
    )

    assert len(seen) >= 2
    assert all(item["in_transaction"] is False for item in seen), seen
    assert all(item["peer_begin_immediate"] is True for item in seen), seen
    assert len(candidates) >= 2
    assert conn.in_transaction is False
    conn.close()


def test_journal_digest_external_failure_leaves_source_pending(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "journal": {
                    "extractor": "llm",
                    "llm_chunk_chars": 80,
                    "allow_heuristic_fallback": False,
                    "llm_retry_delay": 0,
                    "llm_max_attempts": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text(
        "SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8"
    )
    db_path = storage / "memory.sqlite3"
    seed = _open_memory_db(db_path)
    scope = _scope()
    first_id = append_journal_entry(
        seed,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="pending-boundary",
        turn_number=1,
        role="user",
        content="第一段失败前不得把日记条目标成已处理，否则无法重试。",
    )
    second_id = append_journal_entry(
        seed,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="pending-boundary",
        turn_number=2,
        role="user",
        content="第二段同样必须保持 pending，外部失败后由后续 digest 重试。",
    )
    seed.close()

    def failing_call_llm(prompt: str, **kwargs):
        del prompt, kwargs
        raise RuntimeError("injected digest network failure")

    monkeypatch.setattr(journal_module, "call_llm", failing_call_llm)
    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", journal_module._call_llm_with_retries)

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="llm",
        scope=scope,
        interval_label="pending-boundary",
        limit_entries=50,
    )

    assert result.get("ok") is False
    verify = sqlite3.connect(db_path)
    try:
        pending_ids = {
            int(row[0])
            for row in verify.execute(
                "SELECT id FROM journal_entries WHERE processed_run_id = ''"
            ).fetchall()
        }
        memories = verify.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        verify.close()
    assert first_id in pending_ids
    assert second_id in pending_ids
    assert memories == 0


def test_nightly_collect_candidates_release_truth_transaction(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nightly_boundary_probe(label TEXT PRIMARY KEY)"
    )
    conn.execute("INSERT OR IGNORE INTO nightly_boundary_probe(label) VALUES ('held')")
    assert conn.in_transaction is True

    bundles = [
        SessionBundle(
            id="nightly-boundary-one",
            source="cli",
            title="nightly boundary one",
            messages=[
                MessageRecord(
                    id=11,
                    session_id="nightly-boundary-one",
                    role="user",
                    content=(
                        "第一段 nightly digest 必须在 LLM 回调前释放 truth 写事务，"
                        "否则对端无法 BEGIN IMMEDIATE。"
                    ),
                    timestamp=1.0,
                )
            ],
            is_task=True,
            completed=True,
        ),
        SessionBundle(
            id="nightly-boundary-two",
            source="cli",
            title="nightly boundary two",
            messages=[
                MessageRecord(
                    id=22,
                    session_id="nightly-boundary-two",
                    role="user",
                    content=(
                        "第二段候选同样要单独走完网络回调；第一段 DML 不能把第二段 "
                        "LLM 藏进同一个长事务。"
                    ),
                    timestamp=2.0,
                )
            ],
            is_task=True,
            completed=True,
        ),
    ]
    seen: list[dict[str, object]] = []
    call_index = {"n": 0}

    def fake_call_llm(prompt: str, **kwargs):
        del prompt, kwargs
        call_index["n"] += 1
        in_txn = bool(conn.in_transaction)
        peer = sqlite3.connect(db_path, timeout=0.2)
        try:
            peer.execute("BEGIN IMMEDIATE")
            peer.rollback()
            peer_ok = True
            peer_error = ""
        except sqlite3.OperationalError as exc:
            peer_ok = False
            peer_error = str(exc)
        finally:
            peer.close()
        seen.append(
            {
                "in_transaction": in_txn,
                "peer_begin_immediate": peer_ok,
                "peer_error": peer_error,
            }
        )
        evidence_id = 22 if call_index["n"] >= 2 else 11
        return json.dumps(
            [
                {
                    "action": "insert",
                    "evidence_message_ids": [evidence_id],
                    "content": (
                        f"nightly digest chunk-{call_index['n']} must persist as "
                        "a durable candidate after the network callback returns."
                    ),
                    "target": "memory",
                    "memory_type": "procedure",
                    "importance": 0.9,
                    "confidence": 0.86,
                    "entities": ["scope-recall", f"chunk-{call_index['n']}"],
                    "tags": ["digest-boundary"],
                    "reason": "LLM extracted a reusable procedure.",
                }
            ]
        )

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", fake_call_llm)

    candidates = nightly_digest.collect_candidates(
        bundles,
        options=DigestOptions(
            hermes_home=tmp_path,
            digest_date=date(2026, 8, 14),
            extractor="llm",
            chunk_chars=80,
            max_session_chars=4000,
            allow_heuristic_fallback=False,
            max_attempts=1,
            retry_delay=0,
        ),
        llm_config={
            "model": "test-model",
            "base_url": "https://example.invalid",
            "api_key": "test-only",
            "api_mode": "chat_completions",
        },
        existing_context=[],
        conn=conn,
    )

    assert len(seen) >= 2
    assert all(item["in_transaction"] is False for item in seen), seen
    assert all(item["peer_begin_immediate"] is True for item in seen), seen
    assert len(candidates) >= 2
    assert conn.in_transaction is False
    conn.close()


def test_apply_journal_candidates_releases_before_vector_embed(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite3"
    conn = _open_memory_db(db_path)
    scope = _scope()
    first_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="apply-embed",
        turn_number=1,
        role="user",
        content="第一段 apply 后的 embedding 不得仍握着写事务。",
    )
    second_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="apply-embed",
        turn_number=2,
        role="user",
        content="第二段 embedding 同样必须允许对端 BEGIN IMMEDIATE。",
    )
    seen: list[dict[str, object]] = []

    def tracking_replay(vector_runtime, deferred_vector_ops, payload):
        del vector_runtime, deferred_vector_ops
        in_txn = bool(conn.in_transaction)
        peer = sqlite3.connect(db_path, timeout=0.2)
        try:
            peer.execute("BEGIN IMMEDIATE")
            peer.rollback()
            peer_ok = True
            peer_error = ""
        except sqlite3.OperationalError as exc:
            peer_ok = False
            peer_error = str(exc)
        finally:
            peer.close()
        seen.append(
            {
                "in_transaction": in_txn,
                "peer_begin_immediate": peer_ok,
                "peer_error": peer_error,
                "memory_id": payload.get("id"),
            }
        )
        return {"claimed": 0, "completed": 0, "failed": 0}

    monkeypatch.setattr(journal_module, "_replay_or_defer_journal_vector", tracking_replay)

    result = apply_journal_candidates(
        conn,
        object(),
        scope,
        run_id="apply-embed-boundary",
        candidates=[
            JournalDigestCandidate(
                content=(
                    "scope-recall digest apply-one must persist as a durable "
                    "candidate after the short apply transaction commits."
                ),
                target="memory",
                memory_type="procedure",
                importance=0.9,
                confidence=0.86,
                entities=["scope-recall", "apply-one"],
                tags=["digest-boundary", "apply-one"],
                reason="first apply candidate",
                entry_ids=[first_id],
                session_ids=["apply-embed"],
            ),
            JournalDigestCandidate(
                content=(
                    "scope-recall digest apply-two must persist as a second "
                    "durable candidate so first DML cannot hide the embed callback."
                ),
                target="memory",
                memory_type="procedure",
                importance=0.9,
                confidence=0.86,
                entities=["scope-recall", "apply-two"],
                tags=["digest-boundary", "apply-two"],
                reason="second apply candidate",
                entry_ids=[second_id],
                session_ids=["apply-embed"],
            ),
        ],
    )

    assert result["counts"].get("inserted", 0) >= 2
    assert len(seen) >= 2
    assert all(item["in_transaction"] is False for item in seen), seen
    assert all(item["peer_begin_immediate"] is True for item in seen), seen
    conn.close()


def test_apply_journal_candidates_caller_owned_rollback_leaves_no_durable_truth(
    tmp_path, monkeypatch
):
    """Caller-owned BEGIN IMMEDIATE must survive apply; rollback undoes the UoW.

    Status stays applied_pending_outer_commit. The helper must not commit or
    drain derived vector work as if that pending contract were already durable.
    """

    from scope_recall.vector_generation import ensure_vector_generation_schema

    db_path = tmp_path / "memory.sqlite3"
    conn = _open_memory_db(db_path)
    ensure_vector_generation_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("current_generation", "gen-caller-owned", "2026-08-18T00:00:00+00:00"),
    )
    conn.commit()
    scope = _scope()
    local_scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=local_scope_id,
        shared_scope_id=shared_scope_id,
        session_id="caller-owned-rollback",
        turn_number=1,
        role="user",
        content="I now live in Bangalore; please keep this current.",
    )
    baseline = {
        "memories": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
        "receipts": conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0],
        "outbox": conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0],
        "watermark": conn.execute(
            "SELECT processed_run_id FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0],
    }
    drain_calls: list[int] = []

    def tracking_drain(vector_runtime, deferred_vector_ops):
        del vector_runtime
        drain_calls.append(len(deferred_vector_ops or []))
        return {"claimed": 0, "completed": 0, "failed": 0}

    monkeypatch.setattr(journal_module, "_drain_deferred_journal_vector", tracking_drain)
    candidate = JournalDigestCandidate(
        content="Benchmark User currently lives in Bangalore.",
        target="user",
        memory_type="factual",
        importance=0.9,
        confidence=0.99,
        reason="caller-owned journal rollback regression",
        entry_ids=[entry_id],
        session_ids=["caller-owned-rollback"],
        evolution=EvolutionProposal(
            action=EvolutionAction.ADD,
            raw_action="add",
            claim=ClaimDraft.from_parts(
                subject="Benchmark User",
                predicate="lives in",
                value="Bangalore",
                scope_id=local_scope_id,
                cardinality="single",
                valid_from="2026-07-01T00:00:00+00:00",
            ),
            evidence_refs=(
                EvidenceReference(
                    source_type="user_message",
                    source_id="caller-owned-journal-message",
                    quote="I now live in Bangalore; please keep this current.",
                    speaker_subject="Benchmark User",
                ),
            ),
            confidence=0.99,
            reason="caller-owned journal rollback regression",
            source="journal-digest",
        ),
    )
    runtime_config = {
        "fact_evolution": {
            "enabled": True,
            "mode": "auto_apply",
            "journal_mode": "auto_apply",
        }
    }

    conn.execute("BEGIN IMMEDIATE")
    pending = apply_journal_candidates(
        conn,
        object(),
        scope,
        run_id="caller-owned-rollback",
        candidates=[candidate],
        runtime_config=runtime_config,
    )
    assert pending["actions"][0]["status"] == "applied_pending_outer_commit"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == baseline["memories"] + 1
    assert (
        conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0]
        == baseline["receipts"] + 1
    )
    assert (
        conn.execute(
            "SELECT processed_run_id FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0]
        == "caller-owned-rollback"
    )
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] > baseline["outbox"]
    assert drain_calls == []
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == baseline["memories"]
    assert (
        conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0]
        == baseline["receipts"]
    )
    assert (
        conn.execute(
            "SELECT processed_run_id FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0]
        == baseline["watermark"]
    )
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == baseline["outbox"]
    assert drain_calls == []
    conn.close()


def test_apply_journal_candidates_vector_replay_failure_keeps_committed_truth_and_replayable_outbox(
    tmp_path, monkeypatch
):
    from scope_recall.vector_generation import ensure_vector_generation_schema

    db_path = tmp_path / "memory.sqlite3"
    conn = _open_memory_db(db_path)
    ensure_vector_generation_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("current_generation", "gen-c1", "2026-08-18T00:00:00+00:00"),
    )
    conn.commit()
    scope = _scope()
    first_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="apply-vector-fail",
        turn_number=1,
        role="user",
        content="Vector replay failure must not roll back already committed truth rows.",
    )

    def boom_replay(vector_runtime, deferred_vector_ops, payload):
        del vector_runtime, deferred_vector_ops, payload
        raise RuntimeError("injected vector replay failure")

    monkeypatch.setattr(journal_module, "_replay_or_defer_journal_vector", boom_replay)

    with pytest.raises(RuntimeError, match="injected vector replay failure"):
        apply_journal_candidates(
            conn,
            object(),
            scope,
            run_id="apply-vector-fail",
            candidates=[
                JournalDigestCandidate(
                    content=(
                        "scope-recall digest vector-fail must persist as a durable "
                        "candidate even when the later vector drain raises."
                    ),
                    target="memory",
                    memory_type="procedure",
                    importance=0.9,
                    confidence=0.86,
                    entities=["scope-recall", "vector-fail"],
                    tags=["digest-boundary", "vector-fail"],
                    reason="vector fail candidate",
                    entry_ids=[first_id],
                    session_ids=["apply-vector-fail"],
                )
            ],
        )
    conn.close()

    verify = sqlite3.connect(db_path)
    try:
        memories = verify.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        sources = verify.execute("SELECT COUNT(*) FROM memory_journal_sources").fetchone()[0]
        outbox = verify.execute(
            "SELECT COUNT(*) FROM vector_outbox WHERE status = 'pending'"
        ).fetchone()[0]
    finally:
        verify.close()
    assert memories >= 1
    assert sources >= 1
    assert outbox >= 1


def test_run_journal_digest_multi_scope_commits_rejection_before_next_network(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "journal": {
                    "extractor": "llm",
                    "llm_chunk_chars": 80,
                    "llm_max_session_chars": 4000,
                    "allow_heuristic_fallback": False,
                    "llm_max_attempts": 1,
                    "llm_retry_delay": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text(
        "SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8"
    )
    db_path = storage / "memory.sqlite3"
    conn = _open_memory_db(db_path)
    first_scope = RuntimeScope(
        platform="telegram",
        user_id="digest-scope-one",
        chat_id="dm-one",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )
    second_scope = RuntimeScope(
        platform="telegram",
        user_id="digest-scope-two",
        chat_id="dm-two",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )
    first_id = append_journal_entry(
        conn,
        scope=first_scope,
        scope_id=build_scope_id(first_scope),
        shared_scope_id=build_shared_scope_id(first_scope),
        session_id="scope-one-reviewed",
        turn_number=1,
        role="tool",
        content=(
            "Tool execution trace that the digest reviews without producing a "
            "durable memory candidate."
        ),
    )
    second_id = append_journal_entry(
        conn,
        scope=second_scope,
        scope_id=build_scope_id(second_scope),
        shared_scope_id=build_shared_scope_id(second_scope),
        session_id="scope-two-network",
        turn_number=1,
        role="user",
        content=(
            "Joy 要求第二段 journal digest 在外部 LLM 回调期间不得持有 SQLite "
            "写事务，并且第一段 rejection receipt 与 checkpoint 必须已经落盘。"
        ),
    )
    conn.close()

    seen: list[dict[str, object]] = []
    held: dict[str, sqlite3.Connection | None] = {"conn": None}
    real_open = journal_module._open_digest_connection

    def tracking_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        held["conn"] = conn
        return conn

    monkeypatch.setattr(journal_module, "_open_digest_connection", tracking_open)

    def fake_call_llm(prompt: str, **kwargs):
        del prompt, kwargs
        digest_conn = held["conn"]
        in_txn = bool(getattr(digest_conn, "in_transaction", False))
        peer = sqlite3.connect(db_path, timeout=0.2)
        try:
            peer.execute("BEGIN IMMEDIATE")
            peer.rollback()
            peer_ok = True
            peer_error = ""
            rejection = peer.execute(
                "SELECT reason FROM journal_rejections WHERE journal_entry_id = ?",
                (first_id,),
            ).fetchone()
            checkpoint = peer.execute(
                "SELECT processed_run_id FROM journal_entries WHERE id = ?",
                (first_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            peer_ok = False
            peer_error = str(exc)
            rejection = None
            checkpoint = None
        finally:
            peer.close()
        seen.append(
            {
                "in_transaction": in_txn,
                "peer_begin_immediate": peer_ok,
                "peer_error": peer_error,
                "rejection_reason": rejection[0] if rejection else "",
                "checkpoint": checkpoint[0] if checkpoint else "",
            }
        )
        return _candidate_payload(second_id, "scope-two")

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        journal_extractors,
        "_call_llm_with_retries",
        journal_module._call_llm_with_retries,
    )

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="llm",
        interval_label="multi-scope-boundary",
        limit_entries=50,
    )

    assert result.get("ok") is True
    assert len(seen) >= 1
    assert all(item["in_transaction"] is False for item in seen), seen
    assert all(item["peer_begin_immediate"] is True for item in seen), seen
    assert all(
        item["rejection_reason"] == "admission:tool_noise" for item in seen
    ), seen
    assert all(item["checkpoint"] for item in seen), seen
    assert seen[0]["rejection_reason"] == "admission:tool_noise"
    assert seen[0]["checkpoint"]
    verify = sqlite3.connect(db_path)
    try:
        first_row = verify.execute(
            "SELECT processed_run_id FROM journal_entries WHERE id = ?",
            (first_id,),
        ).fetchone()
        rejection = verify.execute(
            "SELECT reason FROM journal_rejections WHERE journal_entry_id = ?",
            (first_id,),
        ).fetchone()
    finally:
        verify.close()
    assert first_row[0] == result["run_id"]
    assert rejection[0] == "admission:tool_noise"


def test_sync_turn_capture_llm_releases_duplicate_journal_transaction(
    tmp_path, monkeypatch
):
    from plugins.memory import load_memory_provider

    import scope_recall.capture_llm as capture_llm
    import scope_recall.provider as provider_module

    hermes_home = tmp_path
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "journal": {"background_digest_enabled": False},
                "capture_llm": {
                    "enabled": True,
                    "min_user_chars": 20,
                    "min_assistant_chars": 30,
                    "api_key": "test-key",
                    "model": "test-model",
                    "base_url": "https://example.invalid",
                    "timeout": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    provider.initialize(
        "capture-boundary",
        hermes_home=str(hermes_home),
        platform="cli",
        user_id="capture-user",
        chat_id="capture-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )
    user_text = (
        "Please remember that Scope Recall capture must release the truth "
        "transaction before the capture LLM callback."
    )
    assistant_text = (
        "Acknowledged with a sufficiently long assistant reply so capture LLM "
        "extraction is eligible for this turn."
    )
    provider.sync_turn(user_text, assistant_text)

    seen: list[dict[str, object]] = []

    def tracking_extract(user_content, assistant_content, config):
        del user_content, assistant_content, config
        conn = provider._conn
        in_txn = bool(getattr(conn, "in_transaction", False))
        peer = sqlite3.connect(storage / "memory.sqlite3", timeout=0.2)
        try:
            peer.execute("BEGIN IMMEDIATE")
            peer.rollback()
            peer_ok = True
            peer_error = ""
        except sqlite3.OperationalError as exc:
            peer_ok = False
            peer_error = str(exc)
        finally:
            peer.close()
        seen.append(
            {
                "in_transaction": in_txn,
                "peer_begin_immediate": peer_ok,
                "peer_error": peer_error,
            }
        )
        raise RuntimeError("injected capture network failure")

    live_provider_module = sys.modules[type(provider).__module__]
    monkeypatch.setattr(capture_llm, "extract_capture_candidates", tracking_extract)
    monkeypatch.setattr(provider_module, "extract_capture_candidates", tracking_extract)
    monkeypatch.setattr(live_provider_module, "extract_capture_candidates", tracking_extract)

    provider.sync_turn(user_text, assistant_text)
    try:
        assert seen, "capture LLM callback must run on the duplicate journal path"
        assert all(item["in_transaction"] is False for item in seen), seen
        assert all(item["peer_begin_immediate"] is True for item in seen), seen
        verify = sqlite3.connect(storage / "memory.sqlite3")
        try:
            pending = verify.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE processed_run_id = ''"
            ).fetchone()[0]
            memories = verify.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        finally:
            verify.close()
        assert pending >= 1
        assert memories == 0
    finally:
        provider.shutdown()
