"""Tests for maintenance-operation response helpers.

They keep dry-run/apply output consistent across scripts and provider tools."""

from __future__ import annotations

import sqlite3

from scope_recall.maintenance_ops import connect_memory_db, effective_apply, json_dumps_stable, make_batch_id, memory_db_path, now_utc_iso


def test_effective_apply_dry_run_wins_over_apply():
    assert effective_apply(apply=True, dry_run=False) is True
    assert effective_apply(apply=True, dry_run=True) is False
    assert effective_apply(apply=False, dry_run=False) is False


def test_memory_db_path_accepts_override_and_hermes_home(tmp_path):
    assert memory_db_path(tmp_path / "home") == tmp_path / "home" / "scope-recall" / "memory.sqlite3"
    assert memory_db_path(tmp_path / "home", db_path=tmp_path / "custom.sqlite3") == tmp_path / "custom.sqlite3"


def test_connect_memory_db_respects_read_only_mode(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    ro = connect_memory_db(db_path, apply=False)
    try:
        try:
            ro.execute("INSERT INTO t(id) VALUES (1)")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
        else:  # pragma: no cover - sqlite should enforce mode=ro
            raise AssertionError("read-only connection allowed mutation")
    finally:
        ro.close()

    rw = connect_memory_db(db_path, apply=True)
    try:
        rw.execute("INSERT INTO t(id) VALUES (1)")
        rw.commit()
    finally:
        rw.close()


def test_json_time_and_batch_helpers_are_stable():
    assert json_dumps_stable({"b": 1, "a": "中文"}) == '{"a": "中文", "b": 1}'
    assert "+" in now_utc_iso()
    batch = make_batch_id(" cleanup ")
    assert batch.startswith("cleanup-")
