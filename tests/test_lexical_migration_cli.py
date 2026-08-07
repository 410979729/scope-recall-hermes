"""Lexical shadow migration CLI safety and rollback tests."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_SHADOW_TABLE,
    current_generation_id,
    generation_status,
)
from scope_recall.maintenance_lease import (
    acquire_activation_lease,
    activation_lease_status,
    release_activation_lease,
)
from scope_recall.maintenance_ops import connect_memory_db
from scope_recall.sql_store import ensure_schema, store_row

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate.lexical_index.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "hermes"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for memory_id, content, timestamp in (
        (
            "target",
            "生产数据库迁移方案：先备份，校验副本，安排切换窗口，并完成回滚演练。",
            "2020-01-01T00:00:00+00:00",
        ),
        (
            "english-target",
            "OAuth redirect validation preserves same-origin transport safety.",
            "2021-01-01T00:00:00+00:00",
        ),
    ):
        store_row(
            conn,
            memory_id=memory_id,
            scope_id="scope-a",
            platform="local",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="",
            gateway_session_key="",
            agent_identity="aria",
            agent_workspace="workspace-a",
            session_id="session-a",
            source="user",
            target="memory",
            content=content,
            metadata=json.dumps({"lifecycle": "promoted"}),
            commit=False,
            timestamp=timestamp,
            enqueue_vector_intent=False,
        )
    for index in range(40):
        store_row(
            conn,
            memory_id=f"noise-{index:02d}",
            scope_id="scope-a",
            platform="local",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="",
            gateway_session_key="",
            agent_identity="aria",
            agent_workspace="workspace-a",
            session_id="session-a",
            source="user",
            target="memory",
            content=f"数据库监控日报第{index:02d}期：数据库容量和告警巡检。",
            metadata=json.dumps({"lifecycle": "promoted"}),
            commit=False,
            timestamp=f"2026-08-05T12:{index:02d}:00+00:00",
            enqueue_vector_intent=False,
        )
    conn.commit()
    conn.close()
    return home, db_path


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--hermes-home",
            str(home),
            "--json",
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _payload(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


def test_cli_dry_run_is_read_only_and_does_not_create_shadow(tmp_path: Path):
    home, db_path = _home(tmp_path)
    before = _sha256(db_path)

    proc = _run(home)

    assert proc.returncode == 0, proc.stderr
    payload = _payload(proc)
    assert payload["dry_run"] is True
    assert payload["status"] == "absent"
    assert payload["writes"] == []
    assert _sha256(db_path) == before
    conn = sqlite3.connect(db_path)
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert LEXICAL_SHADOW_TABLE not in tables


def test_cli_apply_requires_explicit_maintenance_confirmation(tmp_path: Path):
    home, db_path = _home(tmp_path)
    before = _sha256(db_path)

    proc = _run(home, "--apply")

    assert proc.returncode == 2
    payload = _payload(proc)
    assert payload["status"] == "confirmation_required"
    assert payload["backup_path"] == ""
    assert _sha256(db_path) == before
    assert not (db_path.parent / "backups").exists()


def test_owner_token_connection_writes_while_ordinary_writer_is_blocked(
    tmp_path: Path,
):
    _home_path, db_path = _home(tmp_path)
    lease = acquire_activation_lease(db_path)
    try:
        owner = connect_memory_db(
            db_path,
            apply=True,
            lease_token=str(lease["token"]),
        )
        ordinary = connect_memory_db(db_path, apply=True)
        try:
            owner.execute(
                "UPDATE lexical_generation_state SET updated_at=? WHERE key='current'",
                ("owner-write",),
            )
            owner.commit()
            with pytest.raises(sqlite3.DatabaseError):
                ordinary.execute(
                    "UPDATE lexical_generation_state SET updated_at=? WHERE key='current'",
                    ("ordinary-write",),
                )
        finally:
            ordinary.close()
            owner.close()
    finally:
        assert release_activation_lease(lease) is True


def test_cli_apply_rejects_foreign_active_lease_before_backup(tmp_path: Path):
    home, db_path = _home(tmp_path)
    lease = acquire_activation_lease(db_path)
    try:
        proc = _run(home, "--apply", "--maintenance-confirmed")
    finally:
        assert release_activation_lease(lease) is True

    payload = _payload(proc)
    assert proc.returncode == 2
    assert payload["status"] == "lease_conflict"
    assert payload["backup_path"] == ""
    assert not (db_path.parent / "backups").exists()


def test_cli_build_activate_and_rollback_are_backup_first_and_cas_guarded(
    tmp_path: Path,
):
    home, db_path = _home(tmp_path)

    built_proc = _run(
        home,
        "--apply",
        "--maintenance-confirmed",
        "--batch-size",
        "7",
        "--sample-limit",
        "8",
    )
    assert built_proc.returncode == 0, built_proc.stderr
    built = _payload(built_proc)
    assert built["status"] == "ready"
    assert built["quality"]["synthetic_cjk_expected_found"] == 3
    assert Path(built["backup_path"]).is_file()
    assert built["maintenance_lease"] == {
        "acquired": True,
        "guards_installed": True,
        "guards_removed": True,
        "released": True,
    }
    assert activation_lease_status(db_path)["status"] == "absent"

    conflict_proc = _run(
        home,
        "--apply",
        "--maintenance-confirmed",
        "--activate",
        "--expected-current",
        "unexpected",
    )
    assert conflict_proc.returncode == 2
    assert "CAS" in _payload(conflict_proc)["error"]

    activated_proc = _run(
        home,
        "--apply",
        "--maintenance-confirmed",
        "--activate",
        "--expected-current",
        "legacy",
    )
    assert activated_proc.returncode == 0, activated_proc.stderr
    activated = _payload(activated_proc)
    assert activated["status"] == "active"
    assert Path(activated["backup_path"]).is_file()

    rollback_proc = _run(
        home,
        "--apply",
        "--maintenance-confirmed",
        "--rollback",
        "--expected-current",
        LEXICAL_GENERATION_ID,
    )
    assert rollback_proc.returncode == 0, rollback_proc.stderr
    rolled_back = _payload(rollback_proc)
    assert rolled_back["status"] == "legacy"
    assert Path(rolled_back["backup_path"]).is_file()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    manifest = generation_status(conn, LEXICAL_GENERATION_ID)
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    quick_checks = []
    for backup in (built["backup_path"], activated["backup_path"], rolled_back["backup_path"]):
        backup_conn = sqlite3.connect(str(backup))
        quick_checks.append(str(backup_conn.execute("PRAGMA quick_check").fetchone()[0]))
        backup_conn.close()
    conn.close()

    verification_conn = sqlite3.connect(db_path)
    verification_conn.row_factory = sqlite3.Row
    current = current_generation_id(verification_conn)
    verification_conn.close()

    assert current == ""
    assert manifest["status"] == "ready"
    assert {"memories_fts", LEXICAL_SHADOW_TABLE} <= tables
    assert quick_checks == ["ok", "ok", "ok"]


def _load_migration_script() -> Any:
    """Load the migration CLI in-process so race probes can hook its seams."""

    spec = importlib.util.spec_from_file_location(
        "lexical_migration_script_under_test",
        SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load lexical migration script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_insert(db_path: Path, memory_id: str, *, timeout: float) -> str:
    """Attempt one out-of-band writer insert and report its fence outcome."""

    raw = sqlite3.connect(db_path, timeout=timeout)
    raw.row_factory = sqlite3.Row
    try:
        store_row(
            raw,
            memory_id=memory_id,
            scope_id="scope-a",
            platform="local",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="",
            gateway_session_key="",
            agent_identity="audit",
            agent_workspace="tmp",
            session_id="s",
            source="user",
            target="memory",
            content="生产库切换窗口中的并发写入",
            metadata=json.dumps({"lifecycle": "promoted"}),
            commit=False,
            timestamp="2026-01-02T00:00:00+00:00",
            enqueue_vector_intent=False,
        )
        raw.commit()
        return "committed"
    except sqlite3.OperationalError as exc:
        return f"blocked:{exc}"
    finally:
        raw.close()


def _run_script_in_process(module: Any, home: Path) -> tuple[int, dict[str, Any]]:
    argv = [
        str(SCRIPT),
        "--hermes-home",
        str(home),
        "--apply",
        "--maintenance-confirmed",
        "--batch-size",
        "10",
        "--sample-limit",
        "1",
        "--json",
    ]
    stdout = io.StringIO()
    old_argv = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(stdout):
            exit_code = int(module.main())
    finally:
        sys.argv = old_argv
    return exit_code, json.loads(stdout.getvalue())


def _count_memory(db_path: Path, memory_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _assert_backup_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    boundary: str,
    injection_timeout: float,
) -> None:
    """Inject a raw writer at one fence boundary and require it to be fenced.

    The backup fence contract: a raw writer must never be able to commit
    between the owner's fence acquisition and guard installation, and the
    resulting backup must match the live truth under the same fence.
    """

    home, db_path = _home(tmp_path)
    module = _load_migration_script()
    memory_id = f"raced-{boundary}"
    outcomes: dict[str, str] = {}

    if boundary == "before_backup":
        original_binding = module.lexical_source_binding
        calls = {"count": 0}

        def inject_before_backup(conn: sqlite3.Connection) -> Any:
            calls["count"] += 1
            if calls["count"] == 1:
                outcomes["injection"] = _raw_insert(
                    db_path, memory_id, timeout=injection_timeout
                )
            return original_binding(conn)

        monkeypatch.setattr(module, "lexical_source_binding", inject_before_backup)
    elif boundary == "during_backup":
        original_backup = module.secure_online_backup

        def inject_during_backup(*args: Any, **kwargs: Any) -> Any:
            outcomes["injection"] = _raw_insert(
                db_path, memory_id, timeout=injection_timeout
            )
            return original_backup(*args, **kwargs)

        monkeypatch.setattr(module, "secure_online_backup", inject_during_backup)
    elif boundary == "after_backup_before_guards":
        original_guards = module.ensure_activation_guard_triggers

        def inject_before_guards(*args: Any, **kwargs: Any) -> Any:
            outcomes["injection"] = _raw_insert(
                db_path, memory_id, timeout=injection_timeout
            )
            return original_guards(*args, **kwargs)

        monkeypatch.setattr(
            module, "ensure_activation_guard_triggers", inject_before_guards
        )
    elif boundary == "before_mutation":
        original_schema = module.ensure_schema

        def inject_before_mutation(*args: Any, **kwargs: Any) -> Any:
            outcomes["injection"] = _raw_insert(
                db_path, memory_id, timeout=injection_timeout
            )
            return original_schema(*args, **kwargs)

        monkeypatch.setattr(module, "ensure_schema", inject_before_mutation)
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(f"unknown boundary {boundary}")

    exit_code, payload = _run_script_in_process(module, home)

    assert outcomes.get("injection", "").startswith("blocked:"), (
        f"raw writer was not fenced at {boundary}: {outcomes.get('injection')}"
    )
    assert exit_code == 0, payload
    assert _count_memory(db_path, memory_id) == 0

    backup_path = Path(str(payload["backup_path"]))
    assert _count_memory(backup_path, memory_id) == 0
    guard_triggers = sqlite3.connect(backup_path)
    try:
        trigger_count = int(
            guard_triggers.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'scope_recall_activation_guard_%'"
            ).fetchone()[0]
        )
    finally:
        guard_triggers.close()
    assert trigger_count == 0, "backup must not contain temporary guard triggers"


def test_backup_fence_blocks_raw_writer_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_backup_fence(
        tmp_path, monkeypatch, boundary="before_backup", injection_timeout=0.0
    )


def test_backup_fence_blocks_raw_writer_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_backup_fence(
        tmp_path, monkeypatch, boundary="during_backup", injection_timeout=0.0
    )


def test_backup_fence_blocks_raw_writer_after_backup_before_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_backup_fence(
        tmp_path,
        monkeypatch,
        boundary="after_backup_before_guards",
        injection_timeout=0.0,
    )


def test_backup_fence_blocks_raw_writer_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_backup_fence(
        tmp_path, monkeypatch, boundary="before_mutation", injection_timeout=5.0
    )
