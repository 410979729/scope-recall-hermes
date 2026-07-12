"""Tests for vector repair CLI dry-run/apply behavior, backups, and dimension checks.

They ensure vector repair rebuilds companion state from SQLite truth safely."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import GenerationIdentity, register_generation


ROOT = Path(__file__).resolve().parents[1]
REPAIR_SCRIPT = ROOT / "scripts" / "repair.vector_index.py"
MISSING_ENV = "SCOPE_RECALL_TEST_MISSING_EMBED_KEY"


def _make_home(tmp_path: Path) -> Path:
    hermes_home = tmp_path / "hermes-home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "backend": "sqlite-bruteforce",
                    "table_name": "memories",
                    "index_general": False,
                    "embedder": {
                        "provider": "openai-compatible",
                        "dimensions": 3072,
                        "model": "gemini-embedding-001",
                        "api_key_env": [MISSING_ENV],
                        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    },
                    "fallback_embedder": {
                        "provider": "local-hash",
                        "dimensions": 256,
                        "model": "hash-v1",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="memory-1",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="tool-store",
            target="memory",
            content="Production vector repair should not silently downgrade embedding dimensions.",
        )
    finally:
        conn.close()

    vector_conn = sqlite3.connect(storage_dir / "vector.sqlite3")
    try:
        vector_conn.executescript(
            """
            CREATE TABLE vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO vector_meta(key, value) VALUES ('dimensions', '3072');
            """
        )
        vector_conn.commit()
    finally:
        vector_conn.close()
    return hermes_home


def _make_local_hash_home(tmp_path: Path) -> Path:
    hermes_home = tmp_path / "hermes-local-hash"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "backend": "sqlite-bruteforce",
                    "table_name": "memories",
                    "embedder": {"provider": "local-hash", "dimensions": 16, "model": "hash-v1"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="memory-1",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="tool-store",
            target="memory",
            content="Vector repair default command must be inspect-first and read-only.",
        )
    finally:
        conn.close()
    return hermes_home


def _run_repair(hermes_home: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop(MISSING_ENV, None)
    return subprocess.run(
        [sys.executable, str(REPAIR_SCRIPT), "--hermes-home", str(hermes_home), *extra_args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_repair_vector_index_blocks_fallback_embedder_by_default(tmp_path: Path):
    hermes_home = _make_home(tmp_path)

    result = _run_repair(hermes_home, "--dry-run")

    assert result.returncode == 2, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["primary_available"] is False
    assert payload["fallback_available"] is True
    assert payload["using_fallback"] is True
    assert payload["fallback_allowed"] is False
    assert payload["existing_dimensions"] == 3072
    assert payload["planned_dimensions"] == 256
    assert payload["dimension_mismatch_with_existing"] is True
    assert MISSING_ENV in payload["error"]
    assert "--allow-fallback-embedder" in payload["error"]


def test_repair_vector_index_allows_fallback_only_when_explicit(tmp_path: Path):
    hermes_home = _make_home(tmp_path)

    result = _run_repair(hermes_home, "--dry-run", "--allow-fallback-embedder")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["primary_available"] is False
    assert payload["fallback_available"] is True
    assert payload["using_fallback"] is True
    assert payload["fallback_allowed"] is True
    assert payload["existing_dimensions"] == 3072
    assert payload["planned_dimensions"] == 256
    assert payload["dimension_mismatch_with_existing"] is True
    assert payload["embedder"]["provider"] == "local-hash"
    assert payload["embedder"]["dimensions"] == 256


def test_repair_vector_index_defaults_to_dry_run_without_apply(tmp_path: Path):
    hermes_home = _make_local_hash_home(tmp_path)
    vector_path = hermes_home / "scope-recall" / "vector.sqlite3"

    result = _run_repair(hermes_home)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["rows"] == 1
    assert not vector_path.exists()


def test_repair_vector_index_apply_flag_rebuilds_vector_companion(tmp_path: Path):
    hermes_home = _make_local_hash_home(tmp_path)
    vector_path = hermes_home / "scope-recall" / "vector.sqlite3"

    result = _run_repair(hermes_home, "--apply")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["audit"]["unique_ids"] == 1
    assert vector_path.exists()


def test_repair_vector_index_accepts_json_flag_for_operator_consistency(tmp_path: Path):
    hermes_home = _make_local_hash_home(tmp_path)

    result = _run_repair(hermes_home, "--dry-run", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["rows"] == 1


def test_repair_vector_index_targets_active_generation_and_excludes_candidates(tmp_path: Path):
    hermes_home = _make_local_hash_home(tmp_path)
    storage_dir = hermes_home / "scope-recall"
    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        store_row(
            conn,
            memory_id="candidate-1",
            scope_id="scope-a",
            platform="cli",
            user_id="operator",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="agent",
            agent_workspace="workspace",
            session_id="session",
            source="event-digest",
            target="memory",
            content="candidate must not enter repaired active generation",
        )
        row = conn.execute("SELECT metadata FROM memories WHERE id = 'candidate-1'").fetchone()
        metadata = json.loads(str(row["metadata"] or "{}"))
        metadata["lifecycle"] = "candidate"
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = 'candidate-1'",
            (json.dumps(metadata, sort_keys=True),),
        )
        register_generation(
            conn,
            generation_id="gen-active",
            identity=GenerationIdentity(
                backend="sqlite-bruteforce",
                provider="local-hash",
                model="hash-v1",
                dimensions=16,
                table_name="memories",
            ),
            storage_path="vector-generations/gen-active",
            status="active",
        )
        conn.execute(
            "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
            ("current_generation", "gen-active", "2026-07-10T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    result = _run_repair(hermes_home, "--dry-run")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    expected_root = storage_dir / "vector-generations" / "gen-active"
    assert payload["generation_id"] == "gen-active"
    assert payload["storage_root"] == str(expected_root)
    assert payload["vector_path"] == str(expected_root / "vector.sqlite3")
    assert payload["rows"] == 1

    apply_result = _run_repair(hermes_home, "--apply")
    assert apply_result.returncode == 2
    apply_payload = json.loads(apply_result.stdout)
    assert apply_payload["status"] == "blocked"
    assert "shadow" in apply_payload["error"].lower()
    assert not (expected_root / "vector.sqlite3").exists()
    conn = sqlite3.connect(storage_dir / "memory.sqlite3")
    try:
        pointer = conn.execute(
            "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert pointer == "gen-active"


def test_operator_cli_vector_repair_is_dry_run_first_and_apply_is_explicit():
    import scope_recall.cli as cli

    assert cli._SCRIPT_COMMANDS[("vector", "repair")] == ("repair.vector_index.py", ["--dry-run"])
    assert cli._SCRIPT_COMMANDS[("vector", "repair", "apply")] == ("repair.vector_index.py", ["--apply"])
    assert cli._SCRIPT_COMMANDS[("playbooks", "supersede")] == ("playbooks.py", ["supersede"])
    assert cli._match_script_command(["vector", "repair", "--hermes-home", "/tmp/home"]) == (
        "repair.vector_index.py",
        ["--dry-run", "--hermes-home", "/tmp/home"],
    )
    assert cli._match_script_command(["vector", "repair", "apply", "--hermes-home", "/tmp/home"]) == (
        "repair.vector_index.py",
        ["--apply", "--hermes-home", "/tmp/home"],
    )
    assert cli._match_script_command(["playbooks", "supersede", "--id", "pb_old", "--superseded-by", "pb_new"]) == (
        "playbooks.py",
        ["supersede", "--id", "pb_old", "--superseded-by", "pb_new"],
    )
