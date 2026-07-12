"""Classified, backup-first cleanup tests for historical vector companion debt."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scope_recall.sql_store import ensure_schema, now_iso, store_row
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
from scope_recall.vector_generation import GenerationIdentity, ensure_vector_generation_schema, register_generation
from scope_recall.vector_repair import plan_hidden_vector_companion_repair, repair_hidden_vector_companions, sqlite_truth_hash


def _truth_row(conn: sqlite3.Connection, memory_id: str, *, target: str = "memory", lifecycle: str = "active") -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="scope-a",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="repair-test",
        source="tool-store",
        target=target,
        content=f"Vector repair fixture {memory_id}.",
        metadata={"lifecycle": lifecycle},
    )
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    metadata = json.loads(str(row[0] or "{}"))
    metadata["lifecycle"] = lifecycle
    conn.execute(
        "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now_iso(), memory_id),
    )
    conn.commit()


def _sqlite_companion(path: Path, ids: list[tuple[str, str]]) -> None:
    store = SQLiteBruteForceVectorStore(path, table_name="memories", dimensions=2)
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": memory_id,
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": target,
                    "content": memory_id,
                    "summary": memory_id,
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [0.25, 0.75],
                }
                for memory_id, target in ids
            ]
        )
    finally:
        store.close()


def _sqlite_ids(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [str(row[0]) for row in conn.execute("SELECT id FROM vector_records ORDER BY id")]
    finally:
        conn.close()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    hermes_home = tmp_path / "hermes-home"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    truth_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(truth_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _truth_row(conn, "active-memory")
    _truth_row(conn, "archived-memory", lifecycle="archived")
    _truth_row(conn, "general-memory", target="general")

    identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=2,
        metric="cosine",
        prompt_profile="default-v1",
        table_name="memories",
    )
    ensure_vector_generation_schema(conn)
    register_generation(
        conn,
        generation_id="gen-sqlite",
        identity=identity,
        storage_path="vector-generations/gen-sqlite",
        status="ready",
    )
    conn.commit()
    conn.close()

    root_vector = storage / "vector.sqlite3"
    generation_vector = storage / "vector-generations" / "gen-sqlite" / "vector.sqlite3"
    _sqlite_companion(
        root_vector,
        [
            ("active-memory", "memory"),
            ("archived-memory", "memory"),
            ("general-memory", "general"),
            ("orphan-memory", "memory"),
        ],
    )
    _sqlite_companion(
        generation_vector,
        [
            ("active-memory", "memory"),
            ("archived-memory", "memory"),
        ],
    )
    return hermes_home, root_vector, generation_vector


def test_hidden_vector_repair_dry_run_classifies_without_writes(tmp_path: Path) -> None:
    hermes_home, root_vector, generation_vector = _fixture(tmp_path)
    before = {
        str(root_vector): hashlib.sha256(root_vector.read_bytes()).hexdigest(),
        str(generation_vector): hashlib.sha256(generation_vector.read_bytes()).hexdigest(),
    }
    truth_before = sqlite_truth_hash(hermes_home / "scope-recall" / "memory.sqlite3")

    plan = plan_hidden_vector_companion_repair(hermes_home, include_policy_excluded=False)

    by_path = {str(item["path"]): item for item in plan["companions"]}
    assert set(by_path) == {str(root_vector), str(generation_vector)}
    root = by_path[str(root_vector)]
    assert root["terminal_hidden_count"] == 1
    assert root["policy_excluded_count"] == 1
    assert root["orphan_count"] == 1
    assert root["planned_delete_count"] == 2
    generation = by_path[str(generation_vector)]
    assert generation["terminal_hidden_count"] == 1
    assert generation["orphan_count"] == 0
    assert generation["planned_delete_count"] == 1
    assert plan["dry_run"] is True
    assert plan["truth_sha256_before"] == truth_before
    assert not (hermes_home / "scope-recall" / "backups").exists()
    assert hashlib.sha256(root_vector.read_bytes()).hexdigest() == before[str(root_vector)]
    assert hashlib.sha256(generation_vector.read_bytes()).hexdigest() == before[str(generation_vector)]


def test_hidden_vector_repair_apply_is_backup_first_verified_and_idempotent(tmp_path: Path) -> None:
    hermes_home, root_vector, generation_vector = _fixture(tmp_path)
    truth_path = hermes_home / "scope-recall" / "memory.sqlite3"
    truth_before = sqlite_truth_hash(truth_path)

    result = repair_hidden_vector_companions(
        hermes_home,
        include_policy_excluded=False,
        apply=True,
        quiescent_confirmed=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["truth_sha256_before"] == truth_before
    assert result["truth_sha256_after"] == truth_before
    assert result["deleted"] == 3
    assert _sqlite_ids(root_vector) == ["active-memory", "general-memory"]
    assert _sqlite_ids(generation_vector) == ["active-memory"]
    backup_root = Path(result["backup_root"])
    assert backup_root.is_dir()
    assert (backup_root / "receipt.json").is_file()
    assert len(result["backups"]) == 2

    repeated = repair_hidden_vector_companions(
        hermes_home,
        include_policy_excluded=False,
        apply=True,
        quiescent_confirmed=True,
    )
    assert repeated["ok"] is True
    assert repeated["deleted"] == 0
    assert repeated["truth_sha256_after"] == truth_before
    assert _sqlite_ids(root_vector) == ["active-memory", "general-memory"]
    assert _sqlite_ids(generation_vector) == ["active-memory"]


def test_hidden_vector_repair_handles_legacy_root_lancedb(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    from scope_recall.vector_store import LanceVectorStore

    hermes_home = tmp_path / "hermes-home"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    truth_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(truth_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _truth_row(conn, "active-memory")
    _truth_row(conn, "archived-memory", lifecycle="archived")
    _truth_row(conn, "general-memory", target="general")
    conn.close()

    vector_dir = storage / "lancedb"
    store = LanceVectorStore(vector_dir, table_name="memories", dimensions=2)
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": memory_id,
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": target,
                    "content": memory_id,
                    "summary": memory_id,
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [0.25, 0.75],
                }
                for memory_id, target in (
                    ("active-memory", "memory"),
                    ("archived-memory", "memory"),
                    ("general-memory", "general"),
                    ("orphan-memory", "memory"),
                )
            ]
        )
    finally:
        store.close()

    plan = plan_hidden_vector_companion_repair(hermes_home)
    assert plan["ok"] is True
    assert plan["companion_count"] == 1
    assert plan["companions"][0]["backend"] == "lancedb"
    assert plan["companions"][0]["planned_delete_count"] == 2
    assert not (storage / "backups").exists()

    result = repair_hidden_vector_companions(
        hermes_home,
        apply=True,
        quiescent_confirmed=True,
    )
    assert result["ok"] is True
    assert result["deleted"] == 2
    assert len(result["backups"]) == 1
    reopened = LanceVectorStore(vector_dir, table_name="memories", dimensions=2)
    reopened.open()
    try:
        assert reopened.list_ids() == ["active-memory", "general-memory"]
    finally:
        reopened.close()


def test_hidden_vector_repair_cli_defaults_to_dry_run_and_guards_apply(tmp_path: Path) -> None:
    hermes_home, root_vector, generation_vector = _fixture(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "repair.hidden_vector_companions.py"
    before = {
        str(root_vector): hashlib.sha256(root_vector.read_bytes()).hexdigest(),
        str(generation_vector): hashlib.sha256(generation_vector.read_bytes()).hexdigest(),
    }

    planned = subprocess.run(
        [sys.executable, str(script), "--hermes-home", str(hermes_home), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert planned.returncode == 0, planned.stderr
    payload = json.loads(planned.stdout)
    assert payload["dry_run"] is True
    assert payload["planned_delete"] == 3
    assert hashlib.sha256(root_vector.read_bytes()).hexdigest() == before[str(root_vector)]
    assert hashlib.sha256(generation_vector.read_bytes()).hexdigest() == before[str(generation_vector)]

    rejected = subprocess.run(
        [
            sys.executable,
            str(script),
            "--hermes-home",
            str(hermes_home),
            "--apply",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 2
    assert "confirm-quiescent" in json.loads(rejected.stdout)["error"]
    assert hashlib.sha256(root_vector.read_bytes()).hexdigest() == before[str(root_vector)]
    assert hashlib.sha256(generation_vector.read_bytes()).hexdigest() == before[str(generation_vector)]


def test_hidden_vector_repair_apply_requires_quiescent_confirmation(tmp_path: Path) -> None:
    hermes_home, _root_vector, _generation_vector = _fixture(tmp_path)

    with pytest.raises(ValueError, match="quiescent"):
        repair_hidden_vector_companions(
            hermes_home,
            include_policy_excluded=False,
            apply=True,
            quiescent_confirmed=False,
        )


def test_hidden_vector_repair_blocks_in_place_apply_to_active_generation(tmp_path: Path) -> None:
    hermes_home, root_vector, generation_vector = _fixture(tmp_path)
    truth_path = hermes_home / "scope-recall" / "memory.sqlite3"
    conn = sqlite3.connect(truth_path)
    try:
        conn.execute("UPDATE vector_generations SET status = 'active' WHERE generation_id = 'gen-sqlite'")
        conn.execute(
            "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
            ("current_generation", "gen-sqlite", now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    root_before = hashlib.sha256(root_vector.read_bytes()).hexdigest()
    generation_before = hashlib.sha256(generation_vector.read_bytes()).hexdigest()

    plan = plan_hidden_vector_companion_repair(hermes_home)
    generation_item = next(item for item in plan["companions"] if item["path"] == str(generation_vector))
    assert generation_item["active_generation"] is True
    assert generation_item["planned_delete_count"] == 1

    result = repair_hidden_vector_companions(
        hermes_home,
        apply=True,
        quiescent_confirmed=True,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked_active_generation"
    assert result["deleted"] == 0
    assert result["writes"] == []
    assert result["backup_root"] == ""
    assert result["receipt_path"] == ""
    assert hashlib.sha256(root_vector.read_bytes()).hexdigest() == root_before
    assert hashlib.sha256(generation_vector.read_bytes()).hexdigest() == generation_before
    conn = sqlite3.connect(truth_path)
    try:
        assert conn.execute(
            "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
        ).fetchone()[0] == "gen-sqlite"
    finally:
        conn.close()


def test_hidden_vector_repair_filesystem_scan_blocks_dangling_current_generation(tmp_path: Path) -> None:
    hermes_home, root_vector, generation_vector = _fixture(tmp_path)
    truth_path = hermes_home / "scope-recall" / "memory.sqlite3"
    conn = sqlite3.connect(truth_path)
    try:
        conn.execute("DELETE FROM vector_generations WHERE generation_id = 'gen-sqlite'")
        conn.execute(
            "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
            ("current_generation", "gen-sqlite", now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    root_before = hashlib.sha256(root_vector.read_bytes()).hexdigest()
    generation_before = hashlib.sha256(generation_vector.read_bytes()).hexdigest()

    plan = plan_hidden_vector_companion_repair(hermes_home)
    generation_item = next(item for item in plan["companions"] if item["path"] == str(generation_vector))
    assert generation_item["sources"] == ["filesystem-scan"]
    assert generation_item["active_generation"] is True
    assert generation_item["planned_delete_count"] == 1

    result = repair_hidden_vector_companions(
        hermes_home,
        apply=True,
        quiescent_confirmed=True,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked_active_generation"
    assert result["deleted"] == 0
    assert result["writes"] == []
    assert hashlib.sha256(root_vector.read_bytes()).hexdigest() == root_before
    assert hashlib.sha256(generation_vector.read_bytes()).hexdigest() == generation_before
