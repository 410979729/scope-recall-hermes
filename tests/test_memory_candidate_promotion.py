"""Tests for candidate promotion planning, archive-noise choices, and apply behavior.

They protect promoted-only profile behavior from stale or unsafe candidate debt."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scope_recall.candidate_promotion import candidate_debt_report, candidate_rows, classify_candidate_row
from scope_recall.governance_cleanup import governance_audit_coverage_report
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "promote.memory_candidates.py"
DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "doctor.py"


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    scope_id: str = "scope-test",
    target: str = "ops",
    source: str = "journal-digest",
    summary: str = "candidate summary",
    content: str = "candidate content",
    metadata: dict | None = None,
    updated_at: str | None = None,
) -> None:
    at = updated_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "lifecycle": "candidate",
        "memory_type": "workflow",
        "confidence": 0.82,
        "importance": 0.66,
        "evidence_refs": ["journal:fixture"],
        **(metadata or {}),
    }
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, metadata
        ) VALUES (?, ?, '', '', '', '', '', '', '', '', ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            memory_id,
            scope_id,
            source,
            target,
            content,
            summary,
            at,
            at,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()


def test_candidate_classifier_promotes_stable_rows_and_keeps_high_risk(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.")
        _insert_memory(conn, "risky", summary="Risky release", content="Run git push and tag after release approval.")
        safe = conn.execute("SELECT * FROM memories WHERE id='safe'").fetchone()
        risky = conn.execute("SELECT * FROM memories WHERE id='risky'").fetchone()
        safe_decision = classify_candidate_row(safe)
        assert safe_decision.action == "promote"
        assert safe_decision.lane == "promote_safe"
        risky_decision = classify_candidate_row(risky)
        assert risky_decision.action == "keep_candidate"
        assert risky_decision.risk == "high"
        assert risky_decision.lane == "needs_review_high_risk"
    finally:
        conn.close()


def test_candidate_classifier_requires_evidence_before_auto_promotion(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(
            conn,
            "missing-evidence",
            summary="Stable workflow",
            content="Run pytest and doctor before rollout.",
            metadata={"evidence_refs": []},
        )
        row = conn.execute("SELECT * FROM memories WHERE id='missing-evidence'").fetchone()
        decision = classify_candidate_row(row)
    finally:
        conn.close()

    assert decision.action == "keep_candidate"
    assert decision.reason == "missing_evidence_anchor"
    assert decision.risk == "medium"
    assert decision.lane == "needs_review"


def test_candidate_report_keeps_conflicting_candidate_for_review(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(conn, "active", summary="Stable workflow", content="Run pytest and doctor before rollout.", metadata={"lifecycle": "promoted"})
        _insert_memory(conn, "candidate", summary="Stable workflow", content="Run pytest and doctor before rollout.")
        report = candidate_debt_report(conn, limit=10, sample_limit=10)
    finally:
        conn.close()

    assert report["by_lane"]["needs_review"] == 1
    sample = next(item for item in report["samples"] if item["id"] == "candidate")
    assert sample["action"] == "keep_candidate"
    assert sample["reason"] == "active_memory_conflict"
    assert sample["conflict_with"] == "active"


def test_candidate_rows_and_debt_report_respect_scope_ids(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(conn, "candidate-scope-a", scope_id="scope-a", summary="Scope A workflow", content="Run scope A workflow with evidence.")
        _insert_memory(conn, "candidate-scope-b", scope_id="scope-b", summary="Scope B workflow", content="Run scope B workflow with evidence.")

        all_rows = candidate_rows(conn, limit=10)
        scoped_rows = candidate_rows(conn, scope_ids=["scope-a"], limit=10)
        empty_scope_rows = candidate_rows(conn, scope_ids=[], limit=10)
        scoped_report = candidate_debt_report(conn, scope_ids=["scope-a"], limit=10, sample_limit=10)
    finally:
        conn.close()

    assert [(row["id"], row["scope_id"]) for row in all_rows] == [("candidate-scope-a", "scope-a"), ("candidate-scope-b", "scope-b")]
    assert [(row["id"], row["scope_id"]) for row in scoped_rows] == [("candidate-scope-a", "scope-a")]
    assert empty_scope_rows == []
    assert scoped_report["candidate_count"] == 1
    assert scoped_report["samples"][0]["id"] == "candidate-scope-a"
    assert scoped_report["samples"][0]["scope_id"] == "scope-a"


def test_store_row_dedup_finds_visible_row_behind_hidden_lifecycles(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    content = "Duplicate durable workflow text with enough substance and evidence anchor."
    try:
        store_row(
            conn,
            memory_id="active-original",
            scope_id="scope-test",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="dedup-fixture",
            source="manual",
            target="ops",
            content=content,
            metadata=json.dumps({"lifecycle": "promoted", "memory_type": "workflow"}, ensure_ascii=False),
            allow_duplicate=True,
        )
        conn.execute("UPDATE memories SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = 'active-original'")
        for index in range(20):
            lifecycle = "candidate" if index % 2 else "archived"
            memory_id = f"hidden-{index:02d}"
            store_row(
                conn,
                memory_id=memory_id,
                scope_id="scope-test",
                platform="telegram",
                user_id="joy",
                chat_id="dm",
                thread_id="",
                gateway_session_key="",
                agent_identity="yuheng",
                agent_workspace="hermes",
                session_id="dedup-fixture",
                source="manual",
                target="ops",
                content=content,
                metadata=json.dumps({"lifecycle": lifecycle, "memory_type": "workflow"}, ensure_ascii=False),
                allow_duplicate=True,
            )
            conn.execute(
                "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps({"lifecycle": lifecycle, "memory_type": "workflow"}, ensure_ascii=False),
                    f"2026-01-{index + 2:02d}T00:00:00+00:00",
                    memory_id,
                ),
            )
        conn.commit()

        row_id, _summary, _updated_at, inserted = store_row(
            conn,
            memory_id="new-duplicate",
            scope_id="scope-test",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="dedup-fixture",
            source="manual",
            target="ops",
            content=content,
            metadata=json.dumps({"memory_type": "workflow"}, ensure_ascii=False),
        )
        duplicate_count = conn.execute("SELECT COUNT(*) FROM memories WHERE content = ?", (content,)).fetchone()[0]
    finally:
        conn.close()

    assert row_id == "active-original"
    assert inserted is False
    assert duplicate_count == 21


def test_store_row_dedup_does_not_update_when_only_hidden_lifecycle_exists(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    content = "Archived workflow text should not be reactivated by dedup upsert."
    try:
        store_row(
            conn,
            memory_id="archived-original",
            scope_id="scope-test",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="dedup-hidden-only",
            source="manual",
            target="ops",
            content=content,
            metadata=json.dumps({"lifecycle": "archived", "memory_type": "workflow"}, ensure_ascii=False),
            allow_duplicate=True,
        )
        conn.execute("UPDATE memories SET metadata = ?, updated_at = '2026-01-01T00:00:00+00:00' WHERE id = 'archived-original'", (json.dumps({"lifecycle": "archived", "memory_type": "workflow"}, ensure_ascii=False),))
        conn.commit()

        row_id, _summary, _updated_at, inserted = store_row(
            conn,
            memory_id="new-active",
            scope_id="scope-test",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="dedup-hidden-only",
            source="manual",
            target="ops",
            content=content,
            metadata=json.dumps({"memory_type": "workflow"}, ensure_ascii=False),
        )
        archived = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = 'archived-original'").fetchone()["metadata"])
        total = conn.execute("SELECT COUNT(*) FROM memories WHERE content = ?", (content,)).fetchone()[0]
    finally:
        conn.close()

    assert row_id == "new-active"
    assert inserted is True
    assert archived["lifecycle"] == "archived"
    assert total == 2


def test_candidate_conflict_check_is_scope_isolated(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(
            conn,
            "active-other-scope",
            scope_id="scope-other",
            target="user",
            summary="User prefers concise replies.",
            content="User prefers concise replies.",
            metadata={"lifecycle": "promoted", "memory_type": "preference", "confidence": 0.95, "importance": 0.8},
        )
        _insert_memory(
            conn,
            "candidate-this-scope",
            scope_id="scope-this",
            target="user",
            summary="User prefers concise replies.",
            content="User prefers concise replies.",
            metadata={"memory_type": "preference", "confidence": 0.9, "importance": 0.8},
        )
        cross_scope = conn.execute("SELECT * FROM memories WHERE id='candidate-this-scope'").fetchone()
        cross_scope_decision = classify_candidate_row(cross_scope, conn)

        _insert_memory(
            conn,
            "active-this-scope",
            scope_id="scope-this",
            target="user",
            summary="User prefers concise replies.",
            content="User prefers concise replies.",
            metadata={"lifecycle": "promoted", "memory_type": "preference", "confidence": 0.95, "importance": 0.8},
        )
        same_scope_decision = classify_candidate_row(cross_scope, conn)
    finally:
        conn.close()

    assert cross_scope_decision.action == "promote"
    assert cross_scope_decision.conflict_with == ""
    assert same_scope_decision.action == "keep_candidate"
    assert same_scope_decision.reason == "active_memory_conflict"
    assert same_scope_decision.conflict_with == "active-this-scope"


def test_candidate_conflict_ignores_hidden_lifecycle_rows(tmp_path):
    hidden_lifecycles = ["rejected", "superseded", "obsolete", "scratch", "archived"]
    for lifecycle in hidden_lifecycles:
        db_path = tmp_path / f"memory-{lifecycle}.sqlite3"
        conn = _conn(db_path)
        try:
            _insert_memory(
                conn,
                f"hidden-{lifecycle}",
                scope_id="scope-this",
                target="user",
                summary="User prefers concise replies.",
                content="User prefers concise replies.",
                metadata={"lifecycle": lifecycle, "memory_type": "preference", "confidence": 0.95, "importance": 0.8},
            )
            _insert_memory(
                conn,
                f"candidate-{lifecycle}",
                scope_id="scope-this",
                target="user",
                summary="User prefers concise replies.",
                content="User prefers concise replies.",
                metadata={"memory_type": "preference", "confidence": 0.9, "importance": 0.8},
            )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (f"candidate-{lifecycle}",)).fetchone()
            decision = classify_candidate_row(row, conn)
        finally:
            conn.close()

        assert decision.action == "promote", lifecycle
        assert decision.conflict_with == ""


def test_promote_memory_candidates_dry_run_is_read_only_and_apply_audits(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
        _insert_memory(
            conn,
            "noise",
            summary="Conversation summary",
            content="One-off transcript digest that should not become a durable profile row.",
            metadata={"memory_type": "summary", "confidence": 0.62, "importance": 0.5},
            updated_at=old_at,
        )
    finally:
        conn.close()

    script = _load_script_module("promote_memory_candidates_script", SCRIPT_PATH)
    dry = script.promote_memory_candidates(hermes_home, apply=False)
    assert dry["dry_run"] is True
    assert dry["mutations"]["promoted"] == 1
    assert {item["lane"] for item in dry["reviewed"]} == {"promote_safe", "archive_low_value"}
    assert dry["before"]["candidate_count"] == 2
    assert dry["before"]["by_lane"]["promote_safe"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
        lifecycle = conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0]
        assert lifecycle == "candidate"
    finally:
        conn.close()

    applied = script.promote_memory_candidates(hermes_home, apply=True, scope_ids=["scope-test"], batch_id="batch-test")
    assert applied["dry_run"] is False
    assert applied["mutations"]["promoted"] == 1
    assert applied["mutations"]["archived"] == 0
    assert applied["after"]["candidate_count"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0] == "promoted"
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='noise'").fetchone()[0] == "candidate"
        event = conn.execute("SELECT event_type, action, batch_id, target_id FROM governance_audit_events").fetchone()
        assert event == ("memory_candidate_promotion", "promote", "batch-test", "safe")
    finally:
        conn.close()


def test_promote_memory_candidates_action_filter_can_archive_low_value_lane(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
        _insert_memory(
            conn,
            "noise",
            summary="Conversation summary",
            content="One-off transcript digest that should not become a durable profile row.",
            metadata={"memory_type": "summary", "confidence": 0.62, "importance": 0.5},
            updated_at=old_at,
        )
    finally:
        conn.close()

    script = _load_script_module("promote_memory_candidates_action_filter", SCRIPT_PATH)
    applied = script.promote_memory_candidates(hermes_home, apply=True, scope_ids=["scope-test"], action="archive_low_value", batch_id="archive-low-value")

    assert applied["action_filter"] == "archive_low_value"
    assert applied["mutations"] == {"promoted": 0, "archived": 1, "kept": 0, "skipped": 0}
    assert applied["reviewed"] == [
        {
            "id": "noise",
            "target": "ops",
            "source": "journal-digest",
            "decision": "archive",
            "lane": "archive_low_value",
            "effective_action": "archive",
            "reason": "low_value_memory_type:summary",
            "classifier_reason": "low_value_memory_type:summary",
            "risk": "low",
            "confidence": 0.62,
            "importance": 0.5,
            "memory_type": "summary",
            "evidence_refs": ["journal:fixture"],
            "conflict_with": "",
            "updated_at": old_at,
            "summary": "Conversation summary",
        }
    ]

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0] == "candidate"
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='noise'").fetchone()[0] == "archived"
        event = conn.execute("SELECT event_type, action, batch_id, target_id FROM governance_audit_events").fetchone()
        assert event == ("memory_candidate_promotion", "archive", "archive-low-value", "noise")
    finally:
        conn.close()


def test_promote_memory_candidates_apply_requires_scope_or_explicit_all_scopes(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
    finally:
        conn.close()

    script = _load_script_module("promote_memory_candidates_scope_guard", SCRIPT_PATH)
    blocked = script.promote_memory_candidates(hermes_home, apply=True, batch_id="blocked-no-scope")
    assert blocked["ok"] is False
    assert blocked["error"] == "apply_requires_scope_or_all_scopes"

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0] == "candidate"
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
    finally:
        conn.close()

    applied = script.promote_memory_candidates(hermes_home, apply=True, all_scopes=True, batch_id="explicit-all-scopes")
    assert applied["ok"] is True
    assert applied["scope_filter"] == {"mode": "all", "scope_ids": []}
    assert applied["mutations"]["promoted"] == 1


def test_promote_memory_candidates_cli_dry_run_wins_and_sanitizes_reviewed_summaries(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(
            conn,
            "risky-secret",
            summary="token=" + "gh" + "p_abcdefghijklmnopqrstuvwxyz123456" + " at /home/a/private/file.txt",
            content="Do not promote this credential-like candidate.",
            metadata={"memory_type": "workflow", "confidence": 0.9, "importance": 0.9},
        )
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--hermes-home", str(hermes_home), "--apply", "--dry-run", "--json"],
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    reviewed_summary = payload["reviewed"][0]["summary"]
    assert "ghp_" not in reviewed_summary
    assert "/home/a" not in reviewed_summary
    assert "[REDACTED_SECRET]" in reviewed_summary
    assert "[REDACTED_PATH]" in reviewed_summary

    summary_result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--hermes-home",
            str(hermes_home),
            "--dry-run",
            "--summary-only",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    summary_payload = json.loads(summary_result.stdout)
    assert summary_payload["reviewed_count"] == 1
    assert summary_payload["reviewed_by_effective_action"] == {"keep_candidate": 1}
    assert "reviewed" not in summary_payload
    assert "samples" not in summary_payload["before"]
    assert len(summary_result.stdout) < 5000

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='risky-secret'").fetchone()[0] == "candidate"
    finally:
        conn.close()


def test_promote_memory_candidates_operator_review_ids_file_archives_explicit_candidates(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        _insert_memory(conn, "manual-archive", summary="Risky release", content="Run git push and tag after release approval.")
        _insert_memory(conn, "other", summary="Risky sudo", content="Use sudo only after review.")
    finally:
        conn.close()

    ids_file = tmp_path / "ids.jsonl"
    ids_file.write_text('{"id":"manual-archive"}\n', encoding="utf-8")
    script = _load_script_module("promote_memory_candidates_operator_review", SCRIPT_PATH)
    review_ids = script._load_review_ids_file(str(ids_file))

    dry = script.promote_memory_candidates(
        hermes_home,
        apply=False,
        review_ids=review_ids,
        review_decision="archive",
        review_reason="reviewed as stale release-status task flow",
        batch_id="manual-review",
    )
    assert dry["dry_run"] is True
    assert dry["mutations"] == {"promoted": 0, "archived": 1, "kept": 0, "skipped": 0}
    assert dry["reviewed"][0]["id"] == "manual-archive"
    assert dry["reviewed"][0]["reason"] == "operator_review:reviewed as stale release-status task flow"

    applied = script.promote_memory_candidates(
        hermes_home,
        apply=True,
        scope_ids=["scope-test"],
        review_ids=review_ids,
        review_decision="archive",
        review_reason="reviewed as stale release-status task flow",
        batch_id="manual-review",
    )
    assert applied["mutations"] == {"promoted": 0, "archived": 1, "kept": 0, "skipped": 0}
    assert applied["after"]["candidate_count"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='manual-archive'").fetchone()[0] == "archived"
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='other'").fetchone()[0] == "candidate"
        event = conn.execute("SELECT action, reason, batch_id, target_id FROM governance_audit_events").fetchone()
        assert event == ("archive", "operator_review:reviewed as stale release-status task flow", "manual-review", "manual-archive")
    finally:
        conn.close()


def test_candidate_debt_report_and_doctor_surface_backlog(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
        report = candidate_debt_report(conn)
        assert report["candidate_count"] == 1
        assert report["by_action"]["promote"] == 1
        assert report["by_lane"]["promote_safe"] == 1
        assert report["oldest_age_hours"] >= 24 * 7
    finally:
        conn.close()

    doctor = _load_script_module("doctor_candidate_debt", DOCTOR_PATH)
    payload, check, recommendations = doctor.memory_candidate_debt_report(hermes_home)
    assert check["ok"] is True
    assert payload["candidate_count"] == 1
    assert payload["by_action"]["promote"] == 1
    assert any("promote.memory_candidates.py" in item for item in recommendations)


def test_promote_memory_candidates_does_not_audit_when_update_is_ignored(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
        _insert_memory(conn, "safe", summary="Stable workflow", content="Run pytest and doctor before rollout.", updated_at=old_at)
        conn.execute(
            """
            CREATE TRIGGER ignore_safe_candidate_update
            BEFORE UPDATE OF metadata ON memories
            WHEN OLD.id = 'safe'
            BEGIN
                SELECT RAISE(IGNORE);
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()

    script = _load_script_module("promote_memory_candidates_rowcount", SCRIPT_PATH)
    applied = script.promote_memory_candidates(hermes_home, apply=True, scope_ids=["scope-test"], batch_id="batch-ignored")

    assert applied["mutations"]["promoted"] == 0
    assert applied["mutations"]["skipped"] == 1
    assert applied["reviewed"][0]["effective_action"] == "skip"
    assert applied["reviewed"][0]["skip_reason"] == "conflict"
    assert applied["reviewed"][0]["conflict"]["status"] == "conflict"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT json_extract(metadata, '$.lifecycle') FROM memories WHERE id='safe'").fetchone()[0] == "candidate"
        assert conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE target_id = 'safe'").fetchone()[0] == 0
    finally:
        conn.close()


def test_candidate_promotion_archive_counts_as_governance_audited_archive(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    try:
        _insert_memory(
            conn,
            "noise",
            summary="Conversation summary",
            content="One-off transcript digest that should not become a durable profile row.",
            metadata={"memory_type": "summary", "confidence": 0.62, "importance": 0.5},
            updated_at=old_at,
        )
    finally:
        conn.close()

    script = _load_script_module("promote_memory_candidates_audit_coverage", SCRIPT_PATH)
    script.promote_memory_candidates(hermes_home, apply=True, scope_ids=["scope-test"], action="archive_low_value", batch_id="archive-low-value")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        report = governance_audit_coverage_report(conn, scope_ids=["scope-test"])
    finally:
        conn.close()

    assert report["status"] == "ready"
    assert report["archived_total"] == 1
    assert report["archived_with_audit"] == 1
    assert report["archived_without_audit"] == 0


@pytest.mark.parametrize("configured_backend", ["sqlite-bruteforce", "lancedb"])
def test_bulk_archive_queues_causal_delete_without_direct_vector_mutation(
    tmp_path, configured_backend
):
    hermes_home = tmp_path / "hermes"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "enabled": True,
                    "backend": configured_backend,
                    "fallback_backend": "sqlite-bruteforce",
                }
            }
        ),
        encoding="utf-8",
    )
    truth = _conn(storage_dir / "memory.sqlite3")
    try:
        _insert_memory(truth, "archive-vector")
        bootstrap_legacy_generation(
            truth,
            identity=GenerationIdentity(
                backend="sqlite-bruteforce",
                provider="local-hash",
                model="hash-v1",
                dimensions=256,
            ),
            row_count=1,
        )
        truth.commit()
    finally:
        truth.close()
    vector = sqlite3.connect(storage_dir / "vector.sqlite3")
    try:
        vector.execute("CREATE TABLE vector_records(id TEXT PRIMARY KEY)")
        vector.execute("INSERT INTO vector_records(id) VALUES ('archive-vector')")
        vector.commit()
    finally:
        vector.close()

    script = _load_script_module("promote_memory_candidates_vector_cleanup", SCRIPT_PATH)
    applied = script.promote_memory_candidates(
        hermes_home,
        apply=True,
        scope_ids=["scope-test"],
        review_ids=["archive-vector"],
        review_decision="archive",
        review_reason="reviewed test archive",
    )

    vector = sqlite3.connect(storage_dir / "vector.sqlite3")
    try:
        remaining = int(vector.execute("SELECT COUNT(*) FROM vector_records WHERE id='archive-vector'").fetchone()[0])
    finally:
        vector.close()
    assert applied["vector_cleanup"] == {
        "status": "queued",
        "executor": "vector_outbox",
        "requested": 1,
        "deleted": 0,
    }
    assert remaining == 1
    truth = sqlite3.connect(storage_dir / "memory.sqlite3")
    try:
        outbox = truth.execute(
            "SELECT operation, status FROM vector_outbox WHERE memory_id='archive-vector' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        truth.close()
    assert outbox == ("delete", "pending")
