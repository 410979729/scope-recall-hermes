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
