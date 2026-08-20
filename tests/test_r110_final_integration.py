"""R3 decisive regressions for #44/#45/#46/#48 leave, cursor, retry, and receipt.

These nodes first prove the frozen public-candidate gaps, then stay as the
integration's vertical contract after the synthesized production change.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import get_type_hints

import pytest

import scope_recall.journal as journal_module
import scope_recall.journal_extractors as journal_extractors_module
from scope_recall.journal import (
    append_journal_entry,
    ensure_journal_schema,
    run_journal_digest,
)
from scope_recall.journal_llm import JournalDigestLLMError
from scope_recall.journal_store import load_session_digest_state, load_unprocessed_journal_entries
from scope_recall.models import RuntimeScope
from scope_recall.nightly_digest import SessionChunk
from scope_recall.scope import accessible_scope_ids, build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema


def _scope(*, user_id: str = "r3-test-user-a", chat_id: str = "dm") -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id=user_id,
        chat_id=chat_id,
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )


_IDENTITY_OVERLAP = {
    "cross_platform_shared_scope": True,
    "cli_user_id_fallback": "local",
    "user_aliases": {
        "telegram:r3-test-user-a": "joy",
        "cli:local": "joy",
    },
}


def _home(
    tmp_path: Path, journal_config: dict, extra: dict | None = None
) -> tuple[Path, sqlite3.Connection]:
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    payload = {"vector": {"enabled": False}, "journal": journal_config}
    if extra:
        payload.update(extra)
    (storage / "config.json").write_text(
        json.dumps(payload),
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


def _db(hermes_home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def test_collect_annotation_keeps_structured_extractor_error_honest():
    hints = get_type_hints(journal_module._collect_journal_candidates)
    returns = str(hints.get("return") or "")
    assert "dict" in returns
    raw = journal_module._collect_journal_candidates.__annotations__.get("return")
    assert raw is None or "dict" in str(raw)


def test_loader_partitions_per_scope_session_pair_not_shared_session_string(tmp_path):
    """#46: the same textual session_id in two scopes must not share one cap."""

    _hermes_home, conn = _home(tmp_path, {"max_entries_per_session_per_run": 1})
    scope_a = _scope(user_id="r3-test-user-a")
    scope_b = _scope(user_id="r3-test-user-b")
    a_ids = [
        _append(
            conn,
            scope_a,
            session="shared-session",
            turn=turn,
            content=f"scope-a turn {turn} must keep its own fairness window.",
        )
        for turn in (1, 2)
    ]
    b_ids = [
        _append(
            conn,
            scope_b,
            session="shared-session",
            turn=turn,
            content=f"scope-b turn {turn} must keep its own fairness window.",
        )
        for turn in (1, 2)
    ]
    loaded = load_unprocessed_journal_entries(
        conn,
        scope_ids=[build_scope_id(scope_a), build_scope_id(scope_b)],
        limit=10,
        per_session_limit=1,
    )
    by_pair = {}
    for entry in loaded:
        by_pair.setdefault((entry.scope_id, entry.session_id), []).append(entry.id)
    assert set(by_pair) == {
        (build_scope_id(scope_a), "shared-session"),
        (build_scope_id(scope_b), "shared-session"),
    }
    assert by_pair[(build_scope_id(scope_a), "shared-session")] == [a_ids[0]]
    assert by_pair[(build_scope_id(scope_b), "shared-session")] == [b_ids[0]]


def test_inactive_ready_inventory_lists_every_generation_with_repair(tmp_path):
    """#44: every inactive READY generation is a sanitized inventory row."""

    import test_vector_generation_migration as vgm

    storage, conn, identity, old = vgm._sqlite_fixture(tmp_path)
    active_id = str(old["generation_id"])
    vgm._build_sqlite_ready(storage, conn, identity, old, "gen-doctor-healthy-sibling")
    target = vgm._build_sqlite_ready(storage, conn, identity, old, "gen-doctor-sidecar")
    (target / "vector.sqlite3-wal").write_bytes(b"stale-wal")
    (target / "vector.sqlite3-shm").write_bytes(b"stale-shm")
    conn.close()

    payload, check, _recommendations = vgm._doctor_generation_report(tmp_path, identity)
    assert check["ok"] is True
    inventory = {
        str(item.get("generation_id") or ""): item
        for item in payload.get("inactive_generation_inventory") or []
    }
    assert active_id not in inventory
    assert "gen-doctor-healthy-sibling" in inventory
    assert "gen-doctor-sidecar" in inventory
    broken = inventory["gen-doctor-sidecar"]
    assert broken.get("activatable") is False
    assert broken.get("rebuild_from_sqlite_required") is True
    assert broken.get("repair")
    assert "sqlite" in str(broken.get("repair") or "").lower()
    assert str(target) not in json.dumps(payload, ensure_ascii=False)
    healthy = inventory["gen-doctor-healthy-sibling"]
    assert healthy.get("activatable") is True


def test_raised_extractor_error_without_attempted_ids_charges_no_budget(
    tmp_path, monkeypatch
):
    """#45/#48: a raise with no attempted-ID metadata must fail closed."""

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "retryable_failures_quarantine": 3,
            "extraction_attempts_quarantine": 9,
        },
    )
    scope = _scope()
    digestible_id = _append(
        conn,
        scope,
        session="raise-session",
        turn=1,
        content="这条可抽取记录在缺少 attempted 元数据时不得被猜测计费。",
    )
    evidence_id = _append(
        conn,
        scope,
        session="raise-session",
        turn=2,
        role="tool",
        content="tool execution trace must stay on the admission path.",
    )
    conn.close()

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic extractor collapse without attempted ids")

    monkeypatch.setattr(journal_module, "llm_journal_candidates", boom)
    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="raise", limit_entries=10
    )
    assert result["ok"] is False
    assert result.get("status") == "error"
    actions = json.dumps(result.get("actions") or [], ensure_ascii=False)
    assert "extractor failure" in actions
    conn = _db(hermes_home)
    digestible = conn.execute(
        "SELECT extraction_attempts, retryable_failures, processed_run_id "
        "FROM journal_entries WHERE id=?",
        (digestible_id,),
    ).fetchone()
    evidence = conn.execute(
        "SELECT extraction_attempts, retryable_failures, processed_run_id "
        "FROM journal_entries WHERE id=?",
        (evidence_id,),
    ).fetchone()
    assert int(digestible["extraction_attempts"] or 0) == 0
    assert int(digestible["retryable_failures"] or 0) == 0
    assert not str(digestible["processed_run_id"] or "")
    assert int(evidence["retryable_failures"] or 0) == 0
    assert str(evidence["processed_run_id"] or ""), "evidence stays on admission"
    leave = result.get("leave_states") or {}
    loaded = {digestible_id, evidence_id}
    covered = set()
    for key in ("processed_ids", "retryable_pending_ids", "deferred_ids", "quarantined_ids"):
        covered.update(int(item) for item in leave.get(key) or [])
    assert loaded <= covered
    assert digestible_id in set(leave.get("retryable_pending_ids") or [])
    assert evidence_id in set(leave.get("processed_ids") or [])
    conn.close()


def test_unapplied_candidate_and_dry_run_keep_exclusive_leave(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path, {"extractor": "llm"})
    scope = _scope()
    entry_id = _append(
        conn,
        scope,
        session="dry-leave",
        turn=1,
        content="dry-run 候选未 apply 时也必须落在唯一 leave 分区。",
    )
    conn.close()

    def subset_llm(prompt: str, **kwargs) -> str:
        del prompt, kwargs
        return json.dumps(
            [
                {
                    "action": "insert",
                    "evidence_message_ids": [entry_id],
                    "content": "dry-run candidate must stay pending rather than vanish.",
                    "target": "memory",
                    "memory_type": "procedure",
                    "importance": 0.9,
                    "confidence": 0.86,
                    "entities": ["scope-recall"],
                    "tags": ["dry-run"],
                    "reason": "cited the only loaded row.",
                }
            ]
        )

    monkeypatch.setattr(journal_module, "call_llm", subset_llm)
    result = run_journal_digest(
        hermes_home=hermes_home,
        scope=scope,
        interval_label="dry-leave",
        limit_entries=10,
        dry_run=True,
    )
    leave = result.get("leave_states") or {}
    covered = set()
    groups = []
    for key in ("processed_ids", "retryable_pending_ids", "deferred_ids", "quarantined_ids"):
        group = {int(item) for item in leave.get(key) or []}
        groups.append(group)
        covered.update(group)
    assert covered == {entry_id}
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            assert not (left & right)
    conn = _db(hermes_home)
    row = conn.execute(
        "SELECT processed_run_id, retryable_failures FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert not str(row["processed_run_id"] or "")
    assert int(row["retryable_failures"] or 0) == 0
    conn.close()


def test_two_scope_hard_crash_keeps_partial_receipt_with_first_scope(tmp_path, monkeypatch):
    """E: first-scope commit and its running receipt share one SQLite boundary."""

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
        },
    )
    scope_a = _scope(user_id="r3-test-user-a")
    scope_b = _scope(user_id="r3-test-user-b")
    first_id = _append(
        conn,
        scope_a,
        session="scope-a",
        turn=1,
        content="第一 scope 提交后必须已有匹配的 partial receipt。",
    )
    second_id = _append(
        conn,
        scope_b,
        session="scope-b",
        turn=1,
        content="第二 scope 在硬崩溃前不得出现已提交变更。",
    )
    conn.close()

    def persistent_timeout(*args, **kwargs):
        del args, kwargs
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(
        journal_extractors_module, "_call_llm_with_retries", persistent_timeout
    )

    class SyntheticHardCrash(BaseException):
        """Process-death stand-in that bypasses ordinary Exception receipt handling."""

    commits = {"n": 0}
    real_commit = journal_module._commit_truth_then_drain_vector

    def wrapped(conn_inner, vector_runtime, deferred_ops):
        commits["n"] += 1
        result = real_commit(conn_inner, vector_runtime, deferred_ops)
        if commits["n"] == 1:
            raise SyntheticHardCrash("synthetic hard crash after first-scope commit")
        return result

    monkeypatch.setattr(journal_module, "_commit_truth_then_drain_vector", wrapped)
    with pytest.raises(SyntheticHardCrash):
        run_journal_digest(hermes_home=hermes_home, interval_label="crash", limit_entries=50)
    assert commits["n"] == 1

    conn = _db(hermes_home)
    first = conn.execute(
        "SELECT processed_run_id, deferred_run_id, retryable_failures "
        "FROM journal_entries WHERE id=?",
        (first_id,),
    ).fetchone()
    second = conn.execute(
        "SELECT processed_run_id, deferred_run_id, extraction_attempts, retryable_failures "
        "FROM journal_entries WHERE id=?",
        (second_id,),
    ).fetchone()
    assert int(first["retryable_failures"] or 0) == 1, (
        "first-scope attempted timeout must be committed with the partial receipt"
    )
    assert not str(second["processed_run_id"] or "")
    assert not str(second["deferred_run_id"] or "")
    assert int(second["extraction_attempts"] or 0) == 0
    assert int(second["retryable_failures"] or 0) == 0
    run = conn.execute(
        "SELECT id, status, metadata FROM journal_digest_runs"
    ).fetchone()
    assert run is not None
    assert run["status"] == "running"
    metadata = json.loads(run["metadata"] or "{}")
    assert metadata.get("receipt_kind") == "partial"
    leave = metadata.get("leave_states") or {}
    covered = set()
    for key in ("processed_ids", "retryable_pending_ids", "deferred_ids", "quarantined_ids"):
        covered.update(int(item) for item in leave.get(key) or [])
    assert first_id in covered
    assert second_id not in covered
    from scope_recall.journal_store import load_session_digest_state

    assert load_session_digest_state(
        conn, scope_id=build_scope_id(scope_a), session_id="scope-a"
    ) is not None
    assert load_session_digest_state(
        conn, scope_id=build_scope_id(scope_b), session_id="scope-b"
    ) is None
    conn.close()


def test_doctor_old_schema_is_unavailable_not_numeric_zero(tmp_path):
    """#F: missing deferred/retryable columns must not look like healthy zero."""

    from scope_recall.doctor_journal import journal_report

    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.executescript(
        """
        CREATE TABLE journal_entries (
            id INTEGER PRIMARY KEY,
            scope_id TEXT,
            shared_scope_id TEXT,
            session_id TEXT,
            turn_number INTEGER,
            role TEXT,
            content TEXT,
            content_hash TEXT,
            created_at TEXT,
            processed_run_id TEXT
        );
        CREATE TABLE journal_digest_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            extractor TEXT,
            interval_label TEXT,
            processed_entries INTEGER,
            inserted INTEGER,
            updated INTEGER,
            skipped INTEGER,
            error TEXT,
            metadata TEXT
        );
        CREATE TABLE memory_journal_sources (
            memory_id TEXT,
            journal_entry_id INTEGER,
            run_id TEXT,
            created_at TEXT
        );
        CREATE TABLE journal_rejections (
            journal_entry_id INTEGER,
            run_id TEXT,
            reason TEXT,
            candidate TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    payload, _check, recommendations = journal_report(hermes_home)
    deferred = payload["backlog"]["deferred"]
    retryable = payload["backlog"]["retryable_failures"]
    assert deferred.get("status") in {"schema_missing", "unavailable", "unknown"}
    assert deferred.get("available") is False
    assert deferred.get("count") is None
    assert retryable.get("status") in {"schema_missing", "unavailable", "unknown"}
    assert retryable.get("available") is False
    assert retryable.get("pending_entries") is None or retryable.get("count") is None
    joined = " ".join(recommendations)
    assert "migrat" in joined.lower()
    assert "secret" not in joined.lower()
    assert str(storage) not in joined


def test_doctor_new_schema_zero_is_available_and_distinct_from_missing(tmp_path):
    from scope_recall.doctor_journal import journal_report

    hermes_home, conn = _home(tmp_path, {})
    conn.close()
    payload, check, _recommendations = journal_report(hermes_home)
    assert check["ok"] is True
    deferred = payload["backlog"]["deferred"]
    retryable = payload["backlog"]["retryable_failures"]
    assert deferred.get("available") is True
    assert deferred.get("status") == "available"
    assert deferred.get("count") == 0
    assert retryable.get("available") is True
    assert retryable.get("pending_entries") == 0
    assert "pending_retryable_failures" not in payload["digest_health"]["reasons"]


def test_timeout_then_deterministic_then_timeout_counter_is_one(tmp_path, monkeypatch):
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
            "extraction_attempts_quarantine": 9,
            "retryable_failures_quarantine": 9,
        },
    )
    scope = _scope()
    entry_id = _append(
        conn,
        scope,
        session="consec",
        turn=1,
        content="timeout 后的确定性结果必须清零可重试预算，下一次 timeout 只能是 1。",
    )
    conn.close()
    mode = {"value": "timeout"}

    def scripted(*args, **kwargs):
        del args, kwargs
        if mode["value"] == "timeout":
            raise JournalDigestLLMError(
                "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
            )
        return "[]"

    monkeypatch.setattr(journal_extractors_module, "_call_llm_with_retries", scripted)
    first = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="c1", limit_entries=10
    )
    assert first["ok"] is False
    conn = _db(hermes_home)
    assert int(
        conn.execute(
            "SELECT retryable_failures FROM journal_entries WHERE id=?", (entry_id,)
        ).fetchone()[0]
        or 0
    ) == 1
    conn.close()
    mode["value"] = "empty"
    second = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="c2", limit_entries=10
    )
    assert second["ok"] is True
    conn = _db(hermes_home)
    row = conn.execute(
        "SELECT retryable_failures, extraction_attempts FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert int(row["retryable_failures"] or 0) == 0
    assert int(row["extraction_attempts"] or 0) == 1
    conn.close()
    mode["value"] = "timeout"
    third = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="c3", limit_entries=10
    )
    assert third["ok"] is False
    conn = _db(hermes_home)
    row = conn.execute(
        "SELECT retryable_failures, extraction_attempts FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    assert int(row["retryable_failures"] or 0) == 1
    assert int(row["extraction_attempts"] or 0) == 1
    conn.close()


def _append_owned(
    conn,
    scope,
    *,
    scope_id: str,
    shared_scope_id: str,
    session: str,
    turn: int,
    content: str,
    role: str = "user",
) -> int:
    return append_journal_entry(
        conn,
        scope=scope,
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        session_id=session,
        turn_number=turn,
        role=role,
        content=content,
    )


def test_local_shared_legacy_alias_work_units_claim_each_physical_id_once(
    tmp_path, monkeypatch
):
    """Local/shared/legacy aliases must not extract or charge the same row twice.

    ``_unprocessed_scopes`` groups exact ``scope_id``, then each iteration used
    to load ``accessible_scope_ids``. Those readable aliases overlap, so one
    run could extract the same physical IDs again, double metrics/retry, and
    wrap-replay the same cursor side.
    """

    identity = {"identity": _IDENTITY_OVERLAP}
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
            "extraction_attempts_quarantine": 9,
            "retryable_failures_quarantine": 9,
        },
        extra=identity,
    )
    owner = _scope(user_id="r3-test-user-a")
    other = _scope(user_id="r3-test-user-b")
    local_id = build_scope_id(owner, identity)
    shared_id = build_shared_scope_id(owner, identity)
    legacy_id = build_shared_scope_id(owner)
    readable = set(accessible_scope_ids(owner, identity))
    assert {local_id, shared_id, legacy_id} <= readable

    session = "alias-session"
    owner_ids = []
    for scope_id, label, turns in (
        (local_id, "local", (1, 2)),
        (shared_id, "shared", (3, 4)),
        (legacy_id, "legacy", (5, 6)),
    ):
        for turn in turns:
            owner_ids.append(
                _append_owned(
                    conn,
                    owner,
                    scope_id=scope_id,
                    shared_scope_id=shared_id,
                    session=session,
                    turn=turn,
                    content=(
                        f"{label} alias row {turn} must stay a single physical "
                        "claim across overlapping accessible work units."
                    ),
                )
            )
    other_ids = [
        _append(
            conn,
            other,
            session=session,
            turn=turn,
            content=f"other-scope turn {turn} keeps an independent fairness cursor.",
        )
        for turn in (1, 2)
    ]
    distinct_owners = {
        str(row["scope_id"])
        for row in conn.execute(
            "SELECT DISTINCT scope_id FROM journal_entries WHERE user_id = ?",
            ("r3-test-user-a",),
        )
    }
    assert distinct_owners == {local_id, shared_id, legacy_id}
    conn.close()

    extractor_windows: list[list[int]] = []
    increment_ids: list[int] = []
    real_llm = journal_module.llm_journal_candidates
    real_increment = journal_module.increment_retryable_failures

    def windowed_llm(conn_inner, *, entries, **kwargs):
        extractor_windows.append([int(entry.id) for entry in entries])
        return real_llm(conn_inner, entries=entries, **kwargs)

    def counting_increment(conn_inner, *, entry_ids, commit=True):
        increment_ids.extend(int(entry_id) for entry_id in entry_ids)
        return real_increment(conn_inner, entry_ids=entry_ids, commit=commit)

    def timeout(prompt: str, **kwargs):
        del prompt, kwargs
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(journal_module, "llm_journal_candidates", windowed_llm)
    monkeypatch.setattr(journal_module, "increment_retryable_failures", counting_increment)
    monkeypatch.setattr(
        journal_extractors_module, "_call_llm_with_retries", timeout
    )

    result = run_journal_digest(
        hermes_home=hermes_home, interval_label="alias-overlap", limit_entries=50
    )
    physical = [*owner_ids, *other_ids]
    claimed_from_windows = [entry_id for window in extractor_windows for entry_id in window]
    assert claimed_from_windows, "overlapping aliases must still reach the extractor"
    assert len(claimed_from_windows) == len(set(claimed_from_windows)), (
        "a physical journal id was extracted in more than one work unit"
    )
    assert set(claimed_from_windows) == set(physical)
    assert int(result.get("loaded_entries") or 0) == len(physical)
    assert int(result.get("retryable_failures") or 0) == len(physical)
    assert increment_ids.count(owner_ids[0]) == 1
    for entry_id in physical:
        assert increment_ids.count(entry_id) == 1
        assert claimed_from_windows.count(entry_id) == 1

    conn = _db(hermes_home)
    for entry_id in physical:
        row = conn.execute(
            "SELECT retryable_failures, extraction_attempts, processed_run_id "
            "FROM journal_entries WHERE id=?",
            (entry_id,),
        ).fetchone()
        assert int(row["retryable_failures"] or 0) == 1
        assert int(row["extraction_attempts"] or 0) == 0
        assert not str(row["processed_run_id"] or "")
    owner_cursor = load_session_digest_state(
        conn, scope_id=local_id, session_id=session
    )
    other_cursor = load_session_digest_state(
        conn, scope_id=build_scope_id(other), session_id=session
    )
    assert owner_cursor is not None
    assert other_cursor is not None
    conn.close()


def test_deferred_unattempted_tool_cannot_enter_candidate_provenance(
    tmp_path, monkeypatch
):
    """Budget-deferred tool rows must not hitchhike onto the first candidate.

    ``attach_digestible_tool_provenance`` used to append every digestible tool
    id, including suffix rows that never entered the LLM attempt. Those ids
    then landed in ``entry_ids``, ``memory_journal_sources``, and processed
    state.
    """

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
        },
    )
    scope = _scope()
    session = "tool-prov-session"
    prefix_a = _append(
        conn,
        scope,
        session=session,
        turn=1,
        content="COVERED-PREFIX-A journal digest must keep verified rollback evidence in the attempted window.",
    )
    covered_tool = _append(
        conn,
        scope,
        session=session,
        turn=2,
        role="tool",
        content="rollback verified: the covered prefix restore completed with a guardrail receipt.",
    )
    prefix_b = _append(
        conn,
        scope,
        session=session,
        turn=3,
        content="COVERED-PREFIX-B the attempted suffix of the prefix window still belongs to this extraction.",
    )
    deferred_tool = _append(
        conn,
        scope,
        session=session,
        turn=4,
        role="tool",
        content="verified rollback of the suffix restore guardrail is documented for later replay.",
    )
    suffix = _append(
        conn,
        scope,
        session=session,
        turn=5,
        content="DEFERRED-SUFFIX this overflow row and its tool sibling must stay out of this attempt.",
    )
    conn.close()

    real_chunks = journal_extractors_module.session_chunks

    def prefix_only_chunks(bundle, **kwargs):
        del kwargs
        chunks = real_chunks(bundle, chunk_chars=7000, max_session_chars=16000)
        if not chunks:
            return []
        first = chunks[0]
        return [
            SessionChunk(
                text=first.text,
                message_ids=(prefix_a, prefix_b),
                input_chars=first.input_chars,
                exposed_chars=first.exposed_chars,
                truncated=False,
            )
        ]

    def cite_prefix(prompt: str, **kwargs):
        del prompt, kwargs
        return json.dumps(
            [
                {
                    "action": "insert",
                    "evidence_message_ids": [prefix_a, prefix_b],
                    "content": (
                        "Journal digest must keep verified rollback evidence "
                        "only for the attempted prefix window."
                    ),
                    "target": "memory",
                    "memory_type": "procedure",
                    "importance": 0.9,
                    "confidence": 0.86,
                    "entities": ["scope-recall"],
                    "tags": ["tool-provenance"],
                    "reason": "cited the attempted prefix only.",
                }
            ]
        )

    monkeypatch.setattr(journal_extractors_module, "session_chunks", prefix_only_chunks)
    monkeypatch.setattr(journal_extractors_module, "_call_llm_with_retries", cite_prefix)

    result = run_journal_digest(
        hermes_home=hermes_home,
        scope=scope,
        interval_label="tool-prov",
        limit_entries=20,
    )
    assert result["ok"] is True
    assert int(result.get("inserted") or 0) == 1

    conn = _db(hermes_home)
    sources = {
        int(row["journal_entry_id"])
        for row in conn.execute("SELECT journal_entry_id FROM memory_journal_sources")
    }
    rows = {
        int(row["id"]): row
        for row in conn.execute(
            "SELECT id, processed_run_id, deferred_run_id, "
            "extraction_attempts, retryable_failures FROM journal_entries"
        )
    }
    assert prefix_a in sources
    assert prefix_b in sources
    assert covered_tool in sources
    assert deferred_tool not in sources
    assert suffix not in sources
    assert str(rows[covered_tool]["processed_run_id"] or "")
    assert not str(rows[deferred_tool]["processed_run_id"] or "")
    assert int(rows[deferred_tool]["extraction_attempts"] or 0) == 0
    assert int(rows[deferred_tool]["retryable_failures"] or 0) == 0
    assert int(rows[suffix]["retryable_failures"] or 0) == 0
    assert int(rows[suffix]["extraction_attempts"] or 0) == 0
    leave = result.get("leave_states") or {}
    assert covered_tool in {int(item) for item in leave.get("processed_ids") or []}
    assert deferred_tool not in {int(item) for item in leave.get("processed_ids") or []}
    assert deferred_tool in {int(item) for item in leave.get("deferred_ids") or []}
    conn.close()
