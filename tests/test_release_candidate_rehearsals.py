"""Content-free, isolated evidence contracts for the 2.0.0 release candidate."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sqlite3

from scope_recall.privacy_purge_schema import (
    PRIVACY_PURGE_MIGRATION_ID,
    PRIVACY_PURGE_SCHEMA_VERSION,
)
from scope_recall.relation_policy_generation import (
    RELATION_POLICY_GENERATION_SCHEMA_VERSION,
)
from scope_recall.sqlite_backup import verified_online_backup
from scope_recall.sql_store import ensure_schema, schema_migration_status, store_row


ROOT = Path(__file__).resolve().parents[1]
REHEARSAL_SPEC = ROOT / "scripts" / "release.candidate_rehearsals.json"
EXPECTED_GATES = {
    "activity_snapshot_migration",
    "n_minus_one_n_n_minus_one",
    "purge_restore_replay",
    "readonly_canary",
    "writer_canary",
    "rollback_rehearsal",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_n_minus_one_truth(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_schema(connection)
        store_row(
            connection,
            memory_id="candidate-rehearsal-memory",
            scope_id="candidate-rehearsal-scope",
            platform="test",
            user_id="candidate-rehearsal-user",
            chat_id="candidate-rehearsal-chat",
            thread_id="candidate-rehearsal-thread",
            gateway_session_key="candidate-rehearsal-gateway",
            agent_identity="candidate-rehearsal-agent",
            agent_workspace="candidate-rehearsal-workspace",
            session_id="candidate-rehearsal-session",
            source="release_candidate_rehearsal",
            target="memory",
            content="Release candidate isolated migration marker.",
            timestamp="2026-08-27T00:00:00+00:00",
        )
        for table in (
            "privacy_purge_vector_intents",
            "privacy_purge_source_tombstones",
            "privacy_purge_tombstones",
            "privacy_purge_operations",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute(
            "DELETE FROM schema_migrations WHERE id=?",
            (PRIVACY_PURGE_MIGRATION_ID,),
        )
        connection.execute(
            f"PRAGMA user_version = {RELATION_POLICY_GENERATION_SCHEMA_VERSION}"
        )
        connection.commit()
        before = schema_migration_status(connection)
        assert before["current"] is False
        assert before["missing_migrations"] == [PRIVACY_PURGE_MIGRATION_ID]
    finally:
        connection.close()


def test_activity_snapshot_upgrade_preserves_source_and_payload(tmp_path: Path) -> None:
    source = tmp_path / "activity-n-minus-one.sqlite3"
    snapshot = tmp_path / "isolated-candidate-snapshot.sqlite3"
    _write_n_minus_one_truth(source)
    source_sha256 = _sha256(source)

    receipt = verified_online_backup(source, snapshot)
    assert receipt["logical_equivalent"] is True
    assert receipt["source_health"]["ok"] is True
    assert receipt["backup_health"]["ok"] is True
    assert _sha256(source) == source_sha256

    candidate = sqlite3.connect(snapshot)
    candidate.row_factory = sqlite3.Row
    try:
        before = schema_migration_status(candidate)
        assert before["missing_migrations"] == [PRIVACY_PURGE_MIGRATION_ID]
        ensure_schema(candidate)
        after = schema_migration_status(candidate)
        row = candidate.execute(
            "SELECT content FROM memories WHERE id=?",
            ("candidate-rehearsal-memory",),
        ).fetchone()
        assert after["current"] is True
        assert after["schema_version"] == PRIVACY_PURGE_SCHEMA_VERSION
        assert row is not None
        assert row["content"] == "Release candidate isolated migration marker."
    finally:
        candidate.close()

    assert _sha256(source) == source_sha256


def test_candidate_rehearsal_spec_names_exact_collectable_nodes() -> None:
    payload = json.loads(REHEARSAL_SPEC.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "scope-recall.release-candidate-rehearsals.v1"
    assert payload["candidate_version"] == "2.0.0"
    assert payload["requires_full_ci"] is True
    assert payload["active_instance_allowed"] is False
    gates = payload["gates"]
    assert {gate["id"] for gate in gates} == EXPECTED_GATES

    node_ids = [node for gate in gates for node in gate["node_ids"]]
    assert len(node_ids) == len(set(node_ids))
    for node_id in node_ids:
        relative, separator, function_name = node_id.partition("::")
        assert separator == "::"
        test_path = ROOT / relative
        assert test_path.is_file()
        syntax = ast.parse(test_path.read_text(encoding="utf-8"), filename=relative)
        functions = {
            node.name
            for node in syntax.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert function_name in functions


def test_candidate_evidence_assets_are_release_packaged() -> None:
    import importlib.util

    script = ROOT / "scripts" / "check.release.py"
    spec = importlib.util.spec_from_file_location(
        "scope_recall_candidate_rehearsal_release_check",
        script,
    )
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)

    assert "scripts/report.candidate_manifest.py" in release_check.REQUIRED_SOURCE_FILES
    assert "scripts/release.candidate_rehearsals.json" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/scripts/report.candidate_manifest.py" in release_check.REQUIRED_WHEEL
    assert (
        "scope_recall/scripts/release.candidate_rehearsals.json"
        in release_check.REQUIRED_WHEEL
    )
