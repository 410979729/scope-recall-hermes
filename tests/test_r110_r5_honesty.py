"""R5 decisive honesty contracts: doctor defer, restore matrix, cursor, #44, receipts.

These nodes first prove the post-R4 gaps, then stay as the integration
vertical after the synthesized production change. Packaging is a
non-regression gate only.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

import scope_recall.journal as journal_module
import scope_recall.journal_extractors as journal_extractors_module
from journal_source_restore_support import (
    apply_kwargs,
    build_source_restore_pair,
    checkpoint_sqlite_file,
    journal_content_hash,
    make_exploding_connection_factory,
    nontarget_snapshot,
    open_fixture_connection,
    plan_kwargs,
    rebind_source_expectations,
)
from scope_recall.journal import apply_journal_candidates, run_journal_digest
from scope_recall.journal_candidates import JournalDigestCandidate
from scope_recall.journal_llm import JournalDigestLLMError
from scope_recall.journal_source_restore import (
    _PROTECTED_EPOCH_TABLES,
    run_journal_source_restore,
)
from scope_recall.journal_source_restore_snapshot import _EPOCH_TABLES
from scope_recall.journal_store import load_session_digest_state, upsert_session_digest_state
from scope_recall.maintenance_lease import acquire_activation_lease, release_activation_lease
from test_r110_final_integration import _append, _db, _home, _scope


_OLD_JOURNAL_COLUMNS = ("defer_count", "retryable_failures")
_R4_MANIFEST = {
    "doctor_journal.py": "623ec06e5f8ce4df993e482a12700c00d9ef9ee2e7d7c2ff4c5d8680367a9a9d",
    "doctor_vector.py": "db49eaed2967f56cc3ef8a3d7c66c3db0d075b0b4a77b311ec38224ed99772d2",
    "digest_state.py": "0a6ce502b0ce719b6c47e346862cd5299306734404a77f18e5f4cc510ca02018",
    "journal.py": "0606362289c8668f61911ca47c68cf4d2ecfa252c786bf5110c36806eece859f",
    "journal_source_restore.py": "004cbfde21045238a334808b4c8ef770696b30319c05b90dedbc7a59147e71e6",
    "journal_source_restore_rows.py": "45187241eddc0895d81e6983b32c2ab78093128421d6b734b5bbdd79b7b2eab1",
}


def _run_restore(**kwargs):
    return run_journal_source_restore(**kwargs)


def _apply_restore(pair, **overrides):
    lease = acquire_activation_lease(pair.target_path)
    try:
        kwargs = apply_kwargs(pair)
        kwargs.update(overrides)
        return _run_restore(**kwargs)
    finally:
        release_activation_lease(lease)


def _drop_journal_columns(path: Path, columns: tuple[str, ...] = _OLD_JOURNAL_COLUMNS) -> None:
    conn = open_fixture_connection(path)
    try:
        present = {str(row[1]) for row in conn.execute("PRAGMA table_info(journal_entries)")}
        for name in columns:
            if name in present:
                conn.execute(f"ALTER TABLE journal_entries DROP COLUMN {name}")
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(path)


def _journal_columns(path: Path) -> set[str]:
    conn = open_fixture_connection(path)
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(journal_entries)")}
    finally:
        conn.close()


def test_r4_baseline_manifest_is_recorded():
    """Start-of-R5 hash record of the post-R4 production bytes."""

    assert len(_R4_MANIFEST) == 6


def test_doctor_missing_defer_count_is_unavailable_not_numeric_zero(tmp_path):
    """A. deferred_run_id present but defer_count absent must not look healthy."""

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
            processed_run_id TEXT,
            deferred_run_id TEXT,
            deferred_at TEXT
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
    conn.execute(
        """
        INSERT INTO journal_entries(
            id, scope_id, shared_scope_id, session_id, turn_number, role,
            content, content_hash, created_at, processed_run_id, deferred_run_id
        ) VALUES (1, 's', 'sh', 'sess', 1, 'user', 'old', 'ab', '2026-01-01T00:00:00+00:00', '', 'run-d')
        """
    )
    conn.commit()
    conn.close()

    payload, _check, recommendations = journal_report(hermes_home)
    deferred = payload["backlog"]["deferred"]
    retryable = payload["backlog"]["retryable_failures"]
    assert deferred.get("available") is False
    assert deferred.get("status") in {"schema_missing", "unavailable", "unknown"}
    assert deferred.get("count") is None
    assert deferred.get("repeat_deferred_count") is None
    assert deferred.get("max_defer_count") is None
    assert retryable.get("available") is False
    assert payload["digest_health"]["status"] != "ready"
    assert "deferred_schema_unavailable" in payload["digest_health"]["reasons"]
    joined = " ".join(recommendations)
    assert "migrat" in joined.lower()
    assert "secret" not in joined.lower()
    assert "SELECT" not in joined
    assert "ALTER TABLE" not in joined
    assert str(storage) not in joined


def test_doctor_current_schema_true_zero_stays_available(tmp_path):
    from scope_recall.doctor_journal import journal_report

    hermes_home, conn = _home(tmp_path, {})
    conn.close()
    payload, check, _recommendations = journal_report(hermes_home)
    assert check["ok"] is True
    deferred = payload["backlog"]["deferred"]
    retryable = payload["backlog"]["retryable_failures"]
    assert deferred.get("available") is True
    assert deferred.get("count") == 0
    assert deferred.get("repeat_deferred_count") == 0
    assert deferred.get("max_defer_count") == 0
    assert retryable.get("available") is True
    assert retryable.get("pending_entries") == 0
    assert "deferred_schema_unavailable" not in payload["digest_health"]["reasons"]


@pytest.mark.parametrize(
    "source_old,target_old",
    [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ],
    ids=["old-old", "old-current", "current-old", "current-current"],
)
def test_restore_plan_apply_schema_matrix_is_honest(tmp_path, source_old, target_old):
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.source_path)
    try:
        conn.execute(
            "UPDATE journal_entries SET defer_count=3, retryable_failures=2 "
            "WHERE id = 1016"
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.source_path)
    if source_old:
        _drop_journal_columns(pair.source_path)
    if target_old:
        _drop_journal_columns(pair.target_path)
    pair = rebind_source_expectations(pair)

    plan = _run_restore(**plan_kwargs(pair))
    apply_receipt = _apply_restore(pair)
    mixed = source_old != target_old
    if mixed:
        assert plan["ok"] is False
        assert plan["verdict"] != "ready"
        assert apply_receipt["ok"] is False
        assert apply_receipt["verdict"] != "ready"
        assert plan["error_code"] == apply_receipt["error_code"]
        assert plan["error_code"]
        return
    assert plan["ok"] is True
    assert plan["verdict"] == "ready"
    assert apply_receipt["ok"] is True
    assert apply_receipt["verdict"] == "applied"
    target_cols = _journal_columns(pair.target_path)
    stored = open_fixture_connection(pair.target_path)
    try:
        row = stored.execute(
            "SELECT * FROM journal_entries WHERE content_hash = ?",
            (journal_content_hash("synthetic-approved-16"),),
        ).fetchone()
        assert row is not None
        if not source_old and not target_old:
            assert "defer_count" in target_cols
            assert "retryable_failures" in target_cols
            assert int(row["defer_count"] or 0) == 3
            assert int(row["retryable_failures"] or 0) == 2
        else:
            assert "defer_count" not in target_cols
            assert "retryable_failures" not in target_cols
    finally:
        stored.close()


def test_restore_resets_only_unsafe_unprocessed_cursors_and_reports_receipt(tmp_path):
    pair = build_source_restore_pair(tmp_path, occupy_source_ids=True)
    conn = open_fixture_connection(pair.target_path)
    try:
        upsert_session_digest_state(
            conn,
            scope_id="scope-approved",
            session_id="session-09",
            resume_after_id=9000,
            run_id="pre-unprocessed",
        )
        upsert_session_digest_state(
            conn,
            scope_id="scope-approved",
            session_id="session-01",
            resume_after_id=8000,
            run_id="pre-processed",
        )
        upsert_session_digest_state(
            conn,
            scope_id="scope-unrelated",
            session_id="session-keep",
            resume_after_id=7000,
            run_id="pre-unaffected",
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    pair = rebind_source_expectations(pair)

    plan = _run_restore(**plan_kwargs(pair))
    assert plan["ok"] is True
    assert int(plan.get("cursor_reset_count") or 0) == 0

    before_nontarget = nontarget_snapshot(pair.target_path)
    receipt = _apply_restore(pair)
    assert receipt["ok"] is True
    assert "cursor_reset_count" in receipt
    assert int(receipt["cursor_reset_count"]) >= 1
    assert receipt.get("cursor_reset_digest")
    dumped = json.dumps(receipt, ensure_ascii=False)
    assert "scope-approved" not in dumped
    assert "session-09" not in dumped
    assert "secret" not in dumped.lower()
    assert nontarget_snapshot(pair.target_path) == before_nontarget

    conn = open_fixture_connection(pair.target_path)
    try:
        unprocessed = load_session_digest_state(
            conn, scope_id="scope-approved", session_id="session-09"
        )
        processed = load_session_digest_state(
            conn, scope_id="scope-approved", session_id="session-01"
        )
        unrelated = load_session_digest_state(
            conn, scope_id="scope-unrelated", session_id="session-keep"
        )
        assert unprocessed is None or int(unprocessed["resume_after_id"] or 0) == 0
        assert processed is not None
        assert int(processed["resume_after_id"] or 0) == 8000
        assert unrelated is not None
        assert int(unrelated["resume_after_id"] or 0) == 7000
    finally:
        conn.close()


def test_restore_cursor_reset_rolls_back_with_transaction(tmp_path):
    pair = build_source_restore_pair(tmp_path, occupy_source_ids=True)
    conn = open_fixture_connection(pair.target_path)
    try:
        upsert_session_digest_state(
            conn,
            scope_id="scope-approved",
            session_id="session-09",
            resume_after_id=9000,
            run_id="pre-rollback",
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    pair = rebind_source_expectations(pair)
    receipt = _apply_restore(
        pair,
        connection_factory=make_exploding_connection_factory(fail_on_insert=2),
    )
    assert receipt["ok"] is False
    assert receipt["error_code"] == "apply_rolled_back"
    conn = open_fixture_connection(pair.target_path)
    try:
        cursor = load_session_digest_state(
            conn, scope_id="scope-approved", session_id="session-09"
        )
        assert cursor is not None
        assert int(cursor["resume_after_id"] or 0) == 9000
    finally:
        conn.close()


def test_session_digest_state_is_receipt_excluded_not_invisible():
    from journal_source_restore_support import NONTARGET_TABLES

    assert "journal_session_digest_state" not in _EPOCH_TABLES
    assert "journal_session_digest_state" not in _PROTECTED_EPOCH_TABLES
    assert "journal_session_digest_state" not in NONTARGET_TABLES
    source = (
        Path(__file__).resolve().parents[1] / "journal_source_restore.py"
    ).read_text(encoding="utf-8")
    assert "cursor_reset_count" in source
    assert "journal_session_digest_state" in source


def test_generation_id_sanitizer_never_falls_back_to_original():
    from scope_recall.doctor_vector import (
        _inactive_ready_inventory_item,
        _sanitize_ready_preflight_text,
    )

    samples = [
        r"C:\Users\synthetic\AppData\Local\Temp\screenshot.png",
        r"C:\Users\synthetic\image_cache\img_abc123.png",
        "/home/synthetic/.hermes/image_cache/img_posix.png",
        r"\\fileserver\share\screenshots\shot.png",
        "[Image attached at: C:\\Users\\synthetic\\shot.png]",
        "[screenshot]",
        "sk-" + "A" * 24,
    ]
    for raw in samples:
        cleaned = _sanitize_ready_preflight_text(raw)
        assert raw not in cleaned
        assert cleaned != raw
        item = _inactive_ready_inventory_item(
            generation_id=raw,
            activatable=False,
            reason="probe",
            repair="rebuild",
        )
        dumped = json.dumps(item, ensure_ascii=False)
        assert raw not in dumped
        assert item["generation_id"]
        if not cleaned:
            assert item["generation_id"] != raw


def test_generation_inventory_pathlike_ids_stay_secret_free(tmp_path):
    import test_vector_generation_migration as vgm
    from scope_recall.doctor_vector import _inactive_ready_inventory_item

    raw = "[screenshot]"
    item = _inactive_ready_inventory_item(
        generation_id=raw, activatable=True
    )
    assert item["generation_id"] != raw
    assert raw not in json.dumps(item, ensure_ascii=False)

    storage, conn, identity, old = vgm._sqlite_fixture(tmp_path)
    vgm._build_sqlite_ready(storage, conn, identity, old, "gen-doctor-healthy-sibling")
    conn.close()
    payload, check, _recommendations = vgm._doctor_generation_report(tmp_path, identity)
    assert check["ok"] is True
    inventory = vgm._inventory_by_id(payload)
    assert str(old["generation_id"]) not in inventory
    assert "gen-doctor-healthy-sibling" in inventory
    assert inventory["gen-doctor-healthy-sibling"]["activatable"] is True
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "sk-" not in dumped


def test_ordinary_runtimeerror_after_first_scope_preserves_partial_receipt(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
        },
    )
    scope_a = _scope(user_id="r5-user-a")
    scope_b = _scope(user_id="r5-user-b")
    first_id = _append(
        conn,
        scope_a,
        session="scope-a",
        turn=1,
        content="第一 scope 普通 RuntimeError 后必须保留已提交的 partial receipt。",
    )
    second_id = _append(
        conn,
        scope_b,
        session="scope-b",
        turn=1,
        content="第二 scope 在普通错误前不得出现已提交变更。",
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
    commits = {"n": 0}
    real_commit = journal_module._commit_truth_then_drain_vector

    def wrapped(conn_inner, vector_runtime, deferred_ops):
        commits["n"] += 1
        result = real_commit(conn_inner, vector_runtime, deferred_ops)
        if commits["n"] == 1:
            raise RuntimeError("ordinary failure after first-scope commit")
        return result

    monkeypatch.setattr(journal_module, "_commit_truth_then_drain_vector", wrapped)
    with pytest.raises(RuntimeError, match="ordinary failure after first-scope commit"):
        run_journal_digest(hermes_home=hermes_home, interval_label="r5-err", limit_entries=50)

    conn = _db(hermes_home)
    first = conn.execute(
        "SELECT processed_run_id, retryable_failures FROM journal_entries WHERE id=?",
        (first_id,),
    ).fetchone()
    second = conn.execute(
        "SELECT processed_run_id, deferred_run_id, extraction_attempts, retryable_failures "
        "FROM journal_entries WHERE id=?",
        (second_id,),
    ).fetchone()
    assert int(first["retryable_failures"] or 0) == 1
    assert not str(second["processed_run_id"] or "")
    assert not str(second["deferred_run_id"] or "")
    assert int(second["retryable_failures"] or 0) == 0
    run = conn.execute(
        "SELECT id, status, processed_entries, metadata, error FROM journal_digest_runs"
    ).fetchone()
    assert run is not None
    assert run["status"] == "error"
    assert int(run["processed_entries"] or 0) >= 0
    metadata = json.loads(run["metadata"] or "{}")
    assert metadata.get("receipt_kind") == "partial"
    leave = metadata.get("leave_states") or {}
    covered = set()
    for key in ("processed_ids", "retryable_pending_ids", "deferred_ids", "quarantined_ids"):
        covered.update(int(item) for item in leave.get(key) or [])
    assert first_id in covered
    assert second_id not in covered
    assert run["error"]
    assert "secret" not in str(run["error"]).lower()
    conn.close()


def test_vector_drain_failure_preserves_committed_receipt_and_outbox(
    tmp_path, monkeypatch
):
    from scope_recall.vector_generation import ensure_vector_generation_schema

    hermes_home, conn = _home(
        tmp_path,
        {
            "extractor": "llm",
            "allow_heuristic_fallback": False,
            "llm_max_attempts": 1,
            "llm_retry_delay": 0,
        },
    )
    ensure_vector_generation_schema(conn)
    scope_a = _scope(user_id="r5-user-a")
    scope_b = _scope(user_id="r5-user-b")
    first_id = _append(
        conn,
        scope_a,
        session="scope-a",
        turn=1,
        content="向量 drain 失败后第一 scope 的 truth/outbox 必须仍可回放。",
    )
    second_id = _append(
        conn,
        scope_b,
        session="scope-b",
        turn=1,
        content="第二 scope 在 drain 失败前不得提交。",
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
    commits = {"n": 0}
    real_commit = journal_module._commit_truth_then_drain_vector

    def wrapped(conn_inner, vector_runtime, deferred_ops):
        commits["n"] += 1
        if commits["n"] == 1:
            ensure_vector_generation_schema(conn_inner)
            conn_inner.execute(
                """
                INSERT INTO vector_outbox(
                    event_key, generation_id, memory_id, operation, payload,
                    status, available_at, created_at, updated_at
                ) VALUES (
                    'r5-drain-probe', 'gen-r5', 'mem-r5', 'upsert', '{}',
                    'pending', '2026-08-20T00:00:00+00:00',
                    '2026-08-20T00:00:00+00:00', '2026-08-20T00:00:00+00:00'
                )
                """
            )
        result = real_commit(conn_inner, vector_runtime, deferred_ops)
        if commits["n"] == 1:
            raise RuntimeError("injected vector drain failure")
        return result

    monkeypatch.setattr(journal_module, "_commit_truth_then_drain_vector", wrapped)
    with pytest.raises(RuntimeError, match="injected vector drain failure"):
        run_journal_digest(hermes_home=hermes_home, interval_label="r5-drain", limit_entries=50)

    conn = _db(hermes_home)
    assert int(
        conn.execute(
            "SELECT retryable_failures FROM journal_entries WHERE id=?",
            (first_id,),
        ).fetchone()[0]
        or 0
    ) == 1
    second = conn.execute(
        "SELECT processed_run_id, retryable_failures FROM journal_entries WHERE id=?",
        (second_id,),
    ).fetchone()
    assert not str(second["processed_run_id"] or "")
    assert int(second["retryable_failures"] or 0) == 0
    outbox = conn.execute(
        "SELECT status FROM vector_outbox WHERE event_key='r5-drain-probe'"
    ).fetchone()
    assert outbox is not None
    assert str(outbox["status"] or "") == "pending"
    run = conn.execute(
        "SELECT status, metadata, processed_entries FROM journal_digest_runs"
    ).fetchone()
    assert run["status"] == "error"
    metadata = json.loads(run["metadata"] or "{}")
    assert metadata.get("receipt_kind") == "partial"
    leave = metadata.get("leave_states") or {}
    covered = {
        int(item)
        for key in ("processed_ids", "retryable_pending_ids", "deferred_ids", "quarantined_ids")
        for item in leave.get(key) or []
    }
    assert first_id in covered
    assert second_id not in covered
    conn.close()


def test_pollution_leave_state_is_quarantined_not_processed(tmp_path, monkeypatch):
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
    entry_id = _append(
        conn,
        scope,
        session="pollution",
        turn=1,
        content="这条可抽取记录被污染候选引用后必须只落在 quarantined。",
    )
    conn.close()

    def cite_pollution(prompt: str, **kwargs):
        del prompt, kwargs
        return json.dumps(
            [
                {
                    "action": "insert",
                    "evidence_message_ids": [entry_id],
                    "content": (
                        "Historical Task Snapshot: current_task is T16, the "
                        "worktree is clean, and 170 tests passed before the "
                        "next action."
                    ),
                    "target": "memory",
                    "memory_type": "summary",
                    "importance": 0.9,
                    "confidence": 0.86,
                    "entities": ["scope-recall"],
                    "tags": ["pollution"],
                    "reason": "must be quarantined once.",
                }
            ]
        )

    monkeypatch.setattr(
        journal_extractors_module, "_call_llm_with_retries", cite_pollution
    )
    result = run_journal_digest(
        hermes_home=hermes_home,
        scope=scope,
        interval_label="r5-pollution",
        limit_entries=10,
    )
    leave = result.get("leave_states") or {}
    groups = [
        {int(item) for item in leave.get("processed_ids") or []},
        {int(item) for item in leave.get("retryable_pending_ids") or []},
        {int(item) for item in leave.get("deferred_ids") or []},
        {int(item) for item in leave.get("quarantined_ids") or []},
    ]
    covered = set().union(*groups)
    assert covered == {entry_id}
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            assert not (left & right)
    assert entry_id in groups[3]
    assert entry_id not in groups[0]
    assert int(result.get("counts", {}).get("quarantined") or result.get("quarantined") or 0) >= 1
    conn = _db(hermes_home)
    rejections = list(
        conn.execute(
            "SELECT journal_entry_id, reason FROM journal_rejections WHERE journal_entry_id=?",
            (entry_id,),
        )
    )
    assert len(rejections) == 1
    assert "digest pollution" in str(rejections[0]["reason"])
    conn.close()


def test_apply_pollution_ids_are_not_processed_entry_ids():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from scope_recall.journal_store import ensure_journal_schema
    from scope_recall.sql_store import ensure_schema

    ensure_schema(conn)
    ensure_journal_schema(conn)
    candidate = JournalDigestCandidate(
        content=(
            "Historical Task Snapshot: current_task is T16, the worktree is clean, "
            "and 170 tests passed before the next action."
        ),
        target="memory",
        memory_type="summary",
        entry_ids=[77],
        session_ids=["journal-session"],
    )
    result = apply_journal_candidates(
        conn,
        None,
        _scope(),
        run_id="journal-pollution-r5",
        candidates=[candidate],
        dry_run=False,
        runtime_config={},
    )
    assert result["counts"]["quarantined"] == 1
    assert 77 not in {int(item) for item in result.get("processed_entry_ids") or []}
    assert 77 in {int(item) for item in result.get("pollution_entry_ids") or []}
    rejection = conn.execute(
        "SELECT run_id, reason FROM journal_rejections WHERE journal_entry_id = 77"
    ).fetchone()
    assert rejection is not None
    assert "digest pollution" in rejection["reason"]
    conn.close()


def test_public_archive_excludes_private_and_installs_restore_split(tmp_path):
    import shutil
    import subprocess
    import venv

    from test_journal_source_restore_package import (
        REQUIRED_RUNTIME,
        _build_artifacts,
        _clean_env,
        _venv_python,
    )

    work = Path(tempfile.mkdtemp(prefix="r110-r5-dist-", dir=tempfile.gettempdir()))
    try:
        sdist, wheel = _build_artifacts(work)
        with tarfile.open(sdist, "r:gz") as archive:
            names = set(archive.getnames())
        with zipfile.ZipFile(wheel) as archive:
            wheel_names = set(archive.namelist())
        combined = names | wheel_names
        joined = "\n".join(sorted(combined))
        for banned in (
            ".hermes-agent-src",
            "uv.lock",
            "/.git/",
            "__pycache__",
            ".pytest_cache",
            "beidou_shared_memory.py",
        ):
            assert banned not in joined
        for relative in REQUIRED_RUNTIME:
            assert any(name.endswith(relative) for name in names), relative
            assert any(name.endswith(relative) for name in wheel_names), relative

        venv_dir = work / "sig-venv"
        venv.create(venv_dir, with_pip=True, clear=True)
        python = _venv_python(venv_dir)
        installed = subprocess.run(
            [str(python), "-m", "pip", "install", str(wheel)],
            capture_output=True,
            text=True,
            env=_clean_env(),
            check=False,
        )
        assert installed.returncode == 0, installed.stdout + "\n" + installed.stderr
        probe = subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import inspect, scope_recall.journal_source_restore as a, "
                    "scope_recall.journal_source_restore_rows as b, "
                    "scope_recall.journal_source_restore_snapshot as c; "
                    "assert inspect.signature(a.run_journal_source_restore); "
                    "assert inspect.signature(b.insert_missing_rows); "
                    "assert inspect.signature(c.compute_target_epoch); "
                    "print('ok')"
                ),
            ],
            capture_output=True,
            text=True,
            env=_clean_env(),
            check=False,
        )
        assert probe.returncode == 0, probe.stdout + "\n" + probe.stderr
        assert "ok" in probe.stdout
    finally:
        shutil.rmtree(work, ignore_errors=True)
