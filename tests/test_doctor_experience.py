"""Tests for Experience Kernel doctor checks: duplicate playbooks, review debt, and nightly status.

They make sure Experience operational debt is visible but not auto-mutated."""

from __future__ import annotations

import builtins
import importlib.util
import json
import sqlite3
from pathlib import Path

from scope_recall.journal import append_journal_entry, ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.experience_store import create_playbook, record_playbook_feedback, review_playbook
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "doctor.py"


def _load_doctor_module():
    spec = importlib.util.spec_from_file_location("scope_recall_doctor_experience", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doctor_fallback_redactor_covers_provider_token_shapes(monkeypatch):
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "scope_recall.capture_filters":
            raise ImportError("forced fallback")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    spec = importlib.util.spec_from_file_location("scope_recall_doctor_fallback_redactor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    openai_like = "s" "k-" + "a" * 24
    github_like = "g" "hp_" + "b" * 24
    redacted = module.redact_secret_like_text(f"legacy outcome {openai_like} and {github_like}")

    assert openai_like not in redacted
    assert github_like not in redacted
    assert redacted.count("[REDACTED_SECRET]") == 2


def _payload() -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "headscale_one_way_acl",
        "title": "Headscale one-way ACL",
        "trigger": "User asks for one-way management access.",
        "goal": "Apply one-way isolation safely.",
        "preconditions": [{"id": "p1", "check": "Read live nodes", "evidence_required": "node list"}],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Read policy and live nodes.",
                "evidence_required": "policy path and live nodes",
            }
        ],
        "pitfalls": [],
        "verification": ["checks complete"],
        "cleanup": [],
        "reuse_policy": {"default_decision": "guided_reuse"},
    }


def _scope() -> RuntimeScope:
    return RuntimeScope(platform="telegram", user_id="joy", chat_id="dm", agent_identity="yuheng", agent_workspace="hermes")


def test_nightly_digest_report_ignores_operator_classified_recent_fallbacks(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        conn.execute(
            """
            CREATE TABLE nightly_digest_runs(
                id TEXT PRIMARY KEY,
                digest_date TEXT NOT NULL,
                source_db TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                extractor TEXT NOT NULL,
                model TEXT,
                dry_run INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO nightly_digest_runs(
                id, digest_date, source_db, started_at, finished_at, extractor, model, dry_run,
                status, inserted, updated, skipped, deleted, error, metadata
            ) VALUES (
                'nightly-fallback', '2026-01-02', 'memory.sqlite3', '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:01+00:00',
                'llm', 'fixture', 0, 'ok_with_fallback', 1, 0, 0, 0, NULL, ?
            )
            """,
            (json.dumps({"operator_classification": "accepted_fallback", "classification_reason": "fixture reviewed"}, sort_keys=True),),
        )
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor.nightly_digest_report(tmp_path)

    assert check == {"ok": True, "failures": []}
    assert payload["status"] == "ready"
    assert payload["recent_open_fallbacks"] == 0
    assert payload["recent_historical_fallbacks"] == 1
    assert payload["latest_run"]["operator_classification"] == "accepted_fallback"
    assert not recommendations


def test_doctor_reports_experience_schema_and_counts(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        create_playbook(conn, playbook_id="pb_doc", scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.9)
        review_playbook(conn, playbook_id="pb_doc", accessible_scope_ids=["scope-a"], action="promote", reason="fixture review")
        record_playbook_feedback(conn, playbook_id="pb_doc", scope_id="scope-a", accessible_scope_ids=["scope-a"], outcome="success", evidence=["fixture"])
    finally:
        conn.close()

    payload, check, recommendations = doctor.experience_report(tmp_path)

    assert check == {"ok": True, "failures": []}
    assert payload["status"] == "ready"
    assert payload["playbooks"]["total"] == 1
    assert payload["playbooks"]["by_status"] == {"promoted": 1}
    assert payload["promotion_funnel"]["promoted"] == 1
    assert payload["promotion_funnel"]["promoted_missing_last_verified_at"] == 0
    assert payload["runs"]["total"] == 1
    assert not any("lack last_verified_at" in item for item in recommendations)


def test_doctor_experience_funnel_reports_duplicates_and_review_heavy_state(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        for index in range(3):
            create_playbook(
                conn,
                playbook_id=f"pb_dup_{index}",
                scope_id="scope-a",
                payload=_payload(),
                status="candidate",
                confidence=0.6,
            )
        conn.execute("UPDATE procedural_playbooks SET status = 'needs_review'")
        conn.execute(
            """
            INSERT INTO experience_runs(
                id, playbook_id, scope_id, decision, confidence_at_use, evidence, outcome,
                outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
            ) VALUES ('xrun_misleading', 'pb_dup_0', 'scope-a', 'guided_reuse', 0.5, '[]', 'misleading', 'bad', 'model', 1, 10, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor.experience_report(tmp_path)

    assert check == {"ok": True, "failures": []}
    funnel = payload["promotion_funnel"]
    assert funnel["needs_review"] == 3
    assert funnel["needs_review_ratio"] == 1.0
    assert funnel["duplicate_groups"][0]["count"] == 3
    assert funnel["feedback"]["misleading"] == 1
    assert any("review-heavy" in item for item in recommendations)
    assert any("duplicate" in item.lower() for item in recommendations)
    assert any("misleading" in item for item in recommendations)


def test_doctor_experience_feedback_recommendation_ignores_terminal_reviewed_playbooks(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        create_playbook(conn, playbook_id="pb_bad", scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.6)
        record_playbook_feedback(
            conn,
            playbook_id="pb_bad",
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="misleading",
            evidence=["fixture"],
        )
        review_playbook(
            conn,
            playbook_id="pb_bad",
            accessible_scope_ids=["scope-a"],
            action="quarantine",
            reason="fixture misleading playbook reviewed",
        )
    finally:
        conn.close()

    payload, check, recommendations = doctor.experience_report(tmp_path)

    assert check == {"ok": True, "failures": []}
    feedback = payload["promotion_funnel"]["feedback"]
    assert feedback["misleading"] == 1
    assert feedback["unresolved_misleading"] == 0
    assert not any("misleading" in item for item in recommendations)


def test_doctor_redacts_legacy_secret_like_experience_status_and_outcome(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        create_playbook(conn, playbook_id="pb_doc_secret", scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.9)
        conn.execute("UPDATE procedural_playbooks SET status = ? WHERE id = ?", ("token=legacy_status_example_12345", "pb_doc_secret"))
        conn.execute(
            """
            INSERT INTO experience_runs(
                id, playbook_id, scope_id, decision, confidence_at_use, evidence, outcome,
                outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "xrun_doctor_legacy",
                "pb_doc_secret",
                "scope-a",
                "guided_reuse",
                0.9,
                "[]",
                "token=legacy_outcome_example_12345",
                "safe",
                "model",
                1,
                10,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor.experience_report(tmp_path)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert check == {"ok": True, "failures": []}
    assert recommendations == []
    assert "legacy_status_example_12345" not in serialized
    assert "legacy_outcome_example_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_doctor_reports_missing_experience_schema(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    try:
        conn.execute("CREATE TABLE memories(id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor.experience_report(tmp_path)

    assert check["ok"] is False
    assert payload["status"] == "schema_missing"
    assert "procedural_playbooks" in payload["missing_tables"]
    assert recommendations


def test_doctor_reports_missing_experience_fts_table(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        conn.execute("DROP TABLE procedural_playbooks_fts")
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor.experience_report(tmp_path)

    assert check["ok"] is False
    assert payload["status"] == "schema_missing"
    assert "procedural_playbooks_fts" in payload["missing_tables"]
    assert recommendations


def test_doctor_reports_journal_backlog_distribution_and_threshold_failures(tmp_path):
    doctor = _load_doctor_module()
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    scope = _scope()
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        scope_id = build_scope_id(scope)
        shared_scope_id = build_shared_scope_id(scope)
        for index, role in enumerate(["tool", "tool", "assistant", "user"], start=1):
            append_journal_entry(
                conn,
                scope=scope,
                scope_id=scope_id,
                shared_scope_id=shared_scope_id,
                session_id="backlog-session",
                turn_number=index,
                role=role,
                content=f"Backlog smoke {role} entry {index} with image_cache/img_{index}.jpg marker.",
            )
    finally:
        conn.close()

    payload, check, recommendations = doctor.journal_report(
        tmp_path,
        enabled=True,
        journal_config={"backlog_warn_entries": 2, "backlog_fail_entries": 3, "backlog_max_age_hours": 0},
    )

    assert check["ok"] is False
    assert payload["entries"]["unprocessed"] == 4
    assert payload["backlog"]["unprocessed_by_role"] == {"assistant": 1, "tool": 2, "user": 1}
    assert payload["backlog"]["contamination_counts"]["image_cache/img_"]["unprocessed"] == 4
    assert payload["backlog"]["thresholds"]["fail_entries"] == 3
    assert any("journal backlog has 4 unprocessed" in failure for failure in check["failures"])
    assert any("tool trace hygiene" in item.lower() or "digest" in item.lower() for item in recommendations)
