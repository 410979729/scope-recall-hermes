from __future__ import annotations

import builtins
import importlib.util
import json
import sqlite3
from pathlib import Path

from scope_recall.experience_store import create_playbook, record_playbook_feedback, review_playbook
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
    assert recommendations == []
    assert payload["status"] == "ready"
    assert payload["playbooks"]["total"] == 1
    assert payload["playbooks"]["by_status"] == {"promoted": 1}
    assert payload["runs"]["total"] == 1


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
