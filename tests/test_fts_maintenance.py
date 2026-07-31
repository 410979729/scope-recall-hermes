"""Safe operator repair contracts for lifecycle-aware memory FTS membership."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from scope_recall.fts_maintenance import repair_fts_index
from scope_recall.sql_store import ensure_schema, fts_integrity_report, store_row


def _drifted_home(tmp_path: Path) -> tuple[Path, Path]:
    hermes_home = tmp_path / "hermes-home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    db_path = storage_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="candidate-hidden",
        platform="telegram",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="session-a",
        agent_identity="default",
        agent_workspace="hermes",
        scope_id="scope-a",
        session_id="session-a",
        source="tool-store",
        target="memory",
        content="Candidate hidden memory.",
        metadata=json.dumps({"lifecycle": "candidate"}),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        ("candidate-hidden", "Candidate hidden memory.", "Candidate hidden memory."),
    )
    conn.commit()
    conn.close()
    return hermes_home, db_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fts_repair_dry_run_is_byte_stable(tmp_path):
    hermes_home, db_path = _drifted_home(tmp_path)
    before = _sha256(db_path)

    payload = repair_fts_index(hermes_home, apply=False)

    assert payload["ok"] is False
    assert payload["status"] == "needs_repair"
    assert payload["dry_run"] is True
    assert payload["backup_path"] == ""
    assert payload["before"]["hidden_fts_rows"] == 1
    assert payload["after"] == payload["before"]
    assert _sha256(db_path) == before
    assert not (db_path.parent / "backups").exists()


def test_fts_repair_apply_requires_explicit_maintenance_confirmation(tmp_path):
    hermes_home, db_path = _drifted_home(tmp_path)
    before = _sha256(db_path)

    payload = repair_fts_index(
        hermes_home,
        apply=True,
        maintenance_confirmed=False,
    )

    assert payload["ok"] is False
    assert payload["status"] == "confirmation_required"
    assert payload["dry_run"] is False
    assert payload["backup_path"] == ""
    assert _sha256(db_path) == before


def test_fts_repair_apply_backs_up_then_reconciles(tmp_path):
    hermes_home, db_path = _drifted_home(tmp_path)

    payload = repair_fts_index(
        hermes_home,
        apply=True,
        maintenance_confirmed=True,
    )

    assert payload["ok"] is True
    assert payload["status"] == "ready"
    assert payload["dry_run"] is False
    assert payload["before"]["hidden_fts_rows"] == 1
    assert payload["after"]["healthy"] is True
    assert payload["after"]["fts_rows"] == 0

    backup_path = Path(payload["backup_path"])
    assert backup_path.is_file()
    assert not backup_path.is_symlink()
    assert backup_path.parent.is_dir()
    assert not backup_path.parent.is_symlink()
    if os.name == "nt":
        assert payload["backup_permission_model"] == "windows_acl_inherited"
    else:
        assert payload["backup_permission_model"] == "posix_owner_only"
        assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup_path.parent.stat().st_mode) == 0o700

    backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    backup.row_factory = sqlite3.Row
    try:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert fts_integrity_report(backup)["hidden_fts_rows"] == 1
    finally:
        backup.close()

    current = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    current.row_factory = sqlite3.Row
    try:
        assert fts_integrity_report(current)["healthy"] is True
    finally:
        current.close()
