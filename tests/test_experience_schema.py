"""Tests for Experience schema migrations and table contracts.

They ensure playbook storage can initialize safely in fresh and upgraded SQLite stores."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall.experience_models import ExperienceValidationError, validate_procedural_playbook
from scope_recall.sql_store import BASELINE_MIGRATION_ID, SCHEMA_VERSION, ensure_schema, schema_migration_status


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_ensure_schema_creates_experience_tables_idempotently():
    conn = _conn()

    ensure_schema(conn)
    ensure_schema(conn)

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }

    assert {
        "task_episodes",
        "procedural_playbooks",
        "procedural_playbooks_fts",
        "playbook_versions",
        "experience_runs",
        "reflection_events",
        "fact_freshness",
        "skill_anchors",
        "skill_conflicts",
        "schema_migrations",
    }.issubset(tables)

    status = schema_migration_status(conn)
    assert status["current"] is True
    assert status["schema_version"] == SCHEMA_VERSION
    assert status["user_version"] == SCHEMA_VERSION
    assert status["missing_migrations"] == []
    assert [row["id"] for row in status["applied_migrations"]] == [BASELINE_MIGRATION_ID]
    migration = status["applied_migrations"][0]
    assert migration["plugin_version"] == "1.6.0"
    assert migration["status"] == "applied"
    assert len(migration["checksum"]) == 64

    playbook_columns = {
        row["name"]: row["dflt_value"]
        for row in conn.execute("PRAGMA table_info(procedural_playbooks)").fetchall()
    }
    assert playbook_columns["status"] == "'candidate'"
    assert playbook_columns["confidence"] == "0.50"
    assert playbook_columns["steps"] == "'[]'"
    assert playbook_columns["metadata"] == "'{}'"

    index_names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_experience_playbooks_scope_task_status" in index_names
    assert "idx_experience_runs_playbook_started" in index_names
    assert "idx_fact_freshness_subject" in index_names

    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_schema_migration_status_is_read_only(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        status = schema_migration_status(readonly)
    finally:
        readonly.close()

    assert status["current"] is True
    assert status["user_version"] == SCHEMA_VERSION
    assert status["missing_migrations"] == []


def test_ensure_schema_migrations_never_downgrades_newer_user_version():
    conn = _conn()
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    ensure_schema(conn)
    status = schema_migration_status(conn)

    assert status["user_version"] == SCHEMA_VERSION + 1
    assert status["newer_schema"] is True
    assert status["current"] is False
    assert status["missing_migrations"] == []


def test_schema_migration_status_rejects_corrupt_baseline_metadata():
    conn = _conn()
    ensure_schema(conn)
    conn.execute(
        "UPDATE schema_migrations SET checksum = 'bad', error = 'partial failure' WHERE id = ?",
        (BASELINE_MIGRATION_ID,),
    )

    status = schema_migration_status(conn)

    assert status["current"] is False
    assert status["missing_migrations"] == []
    assert status["invalid_migrations"] == [
        {"id": BASELINE_MIGRATION_ID, "mismatches": ["checksum", "error"]}
    ]


def _valid_playbook_payload() -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "headscale_one_way_acl",
        "title": "Headscale one-way ACL",
        "trigger": "User asks for one-way management access.",
        "goal": "Apply isolation without losing remote access.",
        "preconditions": [
            {
                "id": "p1",
                "check": "Identify target and management nodes from live output.",
                "evidence_required": "headscale/tailscale node list",
            }
        ],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Read current ACL policy and node list.",
                "evidence_required": "policy path and live nodes",
            },
            {
                "number": 2,
                "capability_class": "service_control",
                "action": "Reload policy only after validation and rollback are ready.",
                "evidence_required": "validation output, rollback path, post-reload checks",
            },
        ],
        "pitfalls": [
            {
                "signal": "tailscale status lists a node",
                "mistake": "Assume listing equals reachability",
                "correction": "Use real connectivity checks.",
            }
        ],
        "verification": ["policy check passes", "positive and negative connectivity checks complete"],
        "cleanup": ["Record rollback path and verification output."],
        "reuse_policy": {"default_decision": "guided_reuse"},
    }


def test_validate_procedural_playbook_accepts_capability_classes_and_normalizes_steps():
    payload = _valid_playbook_payload()

    playbook = validate_procedural_playbook(payload)

    assert playbook.task_class == "headscale_one_way_acl"
    assert [step.number for step in playbook.steps] == [1, 2]
    assert [step.capability_class for step in playbook.steps] == ["read_only", "service_control"]
    assert playbook.requires_operator_review is True


@pytest.mark.parametrize("bad_class", ["", "restart_service", "remote_root_shell"])
def test_validate_procedural_playbook_rejects_unknown_capability_classes(bad_class: str):
    payload = _valid_playbook_payload()
    payload["steps"][0]["capability_class"] = bad_class

    with pytest.raises(ExperienceValidationError, match="capability_class"):
        validate_procedural_playbook(payload)


def test_validate_procedural_playbook_rejects_steps_without_evidence_requirement():
    payload = _valid_playbook_payload()
    payload["steps"][0].pop("evidence_required")

    with pytest.raises(ExperienceValidationError, match="evidence_required"):
        validate_procedural_playbook(payload)


@pytest.mark.parametrize("bad_confidence", ["nan", "inf", "-inf"])
def test_validate_procedural_playbook_rejects_non_finite_confidence(bad_confidence: str):
    payload = _valid_playbook_payload()
    payload["confidence"] = bad_confidence

    with pytest.raises(ExperienceValidationError, match="confidence"):
        validate_procedural_playbook(payload)
