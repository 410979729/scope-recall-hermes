"""Tests for Experience replay benchmark cases.

Replay checks verify expected and forbidden procedural matches after retrieval or playbook changes."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scope_recall.experience_bootstrap import bootstrap_core_playbooks
from scope_recall.experience_replay import ReplayCaseValidationError, build_replay_report, coverage_hits, load_replay_cases
from scope_recall.experience_store import create_playbook, review_playbook
from scope_recall.sql_store import ensure_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
EXPERIENCE_REPLAY_CASES = PLUGIN_ROOT / "benchmarks" / "experience_replay_cases.json"


def _payload() -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "headscale_one_way_acl",
        "title": "Headscale one-way ACL rollback-safe procedure",
        "trigger": "Need one-way management access without losing remote connectivity.",
        "goal": "Apply one-way ACL with rollback and negative reachability checks.",
        "preconditions": [
            {"id": "live-nodes", "check": "Read live nodes before editing ACL.", "evidence_required": "live node list"},
            {"id": "rollback", "check": "Prepare rollback command before apply.", "evidence_required": "rollback command"},
        ],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Inspect live Headscale/Tailscale nodes and current policy.",
                "evidence_required": "live node list and policy path",
            },
            {
                "number": 2,
                "capability_class": "local_write",
                "action": "Apply the minimal ACL diff only after rollback is ready.",
                "evidence_required": "rollback command and validation output",
            },
        ],
        "pitfalls": [
            {"signal": "node appears in status", "mistake": "Assume visibility means reachability", "correction": "Run negative reachability checks."}
        ],
        "verification": ["positive path works", "negative reachability is blocked", "rollback command retained"],
        "cleanup": ["Keep rollback command in the run receipt."],
        "reuse_policy": {"default_decision": "direct_reuse", "allow_direct_reuse": True},
    }


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _create_promoted(conn: sqlite3.Connection, *, playbook_id: str = "pb_acl") -> None:
    create_playbook(conn, playbook_id=playbook_id, scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.92)
    review_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=["scope-a"], action="promote", reason="fixture")


def _case() -> dict:
    return {
        "id": "headscale-acl-replay",
        "query": "Need one-way Headscale ACL for management access without reverse access",
        "baseline_text": "I will edit the ACL and test that management reaches the target.",
        "required_terms": ["rollback", "negative reachability", "live nodes"],
        "expected_decision": "direct_reuse",
        "expected_playbook_id": "pb_acl",
    }


def test_build_replay_report_compares_baseline_to_experience_packet_without_mutating_store():
    conn = _conn()
    _create_promoted(conn)
    before_runs = conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0]

    report = build_replay_report(conn, cases=[_case()], accessible_scope_ids=["scope-a"])

    after_runs = conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0]
    assert after_runs == before_runs == 0
    assert report["case_count"] == 1
    assert report["pass_count"] == 1
    assert report["average_with_experience_coverage"] > report["average_baseline_coverage"]
    case = report["cases"][0]
    assert case["passed"] is True
    assert case["playbook_id"] == "pb_acl"
    assert case["decision"] == "direct_reuse"
    assert "rollback" in case["with_experience_hits"]
    assert "negative reachability" in case["with_experience_hits"]
    assert "live nodes" in case["with_experience_hits"]


def test_core_bootstrap_replay_benchmark_covers_positive_and_negative_controls_without_mutating_runs():
    conn = _conn()
    bootstrap = bootstrap_core_playbooks(conn, scope_id="scope-a", shared_scope_id="", accessible_scope_ids=["scope-a"], dry_run=False)
    assert bootstrap["promoted"] >= 5
    before_runs = conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0]
    cases = [
        {
            "id": "core-release-closeout",
            "query": "scope-recall release closeout PyPI 发布前验证",
            "baseline_text": "我会看测试是否通过。",
            "required_terms": ["release gate", "ruff", "pyright", "未获授权不得 push"],
            "expected_decision": "guided_reuse",
            "expected_playbook_id": "pb_core_scope_recall_release_closeout",
            "min_coverage_gain": 0.25,
        },
        {
            "id": "core-journal-backlog",
            "query": "scope recall journal backlog retry dead-letter watermark 修复",
            "baseline_text": "先看 journal 队列。",
            "required_terms": ["retry", "dead-letter", "watermark", "SQLite"],
            "expected_decision": "guided_reuse",
            "expected_playbook_id": "pb_core_journal_backlog_drain",
            "min_coverage_gain": 0.25,
        },
        {
            "id": "negative-unrelated-short",
            "query": "hi",
            "baseline_text": "",
            "required_terms": ["release gate"],
            "expect_no_reuse": True,
        },
    ]

    report = build_replay_report(conn, cases=cases, accessible_scope_ids=["scope-a"])

    assert conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == before_runs
    assert report["case_count"] == 3
    assert report["pass_count"] == 3
    by_id = {case["id"]: case for case in report["cases"]}
    assert by_id["core-release-closeout"]["playbook_id"] == "pb_core_scope_recall_release_closeout"
    assert by_id["core-journal-backlog"]["playbook_id"] == "pb_core_journal_backlog_drain"
    assert by_id["negative-unrelated-short"]["packet_chars"] == 0
    assert by_id["negative-unrelated-short"]["decision"] == "no_reuse"


def test_packaged_core_replay_fixture_passes_against_bootstrapped_playbooks_without_mutating_runs():
    conn = _conn()
    bootstrap = bootstrap_core_playbooks(conn, scope_id="scope-a", shared_scope_id="", accessible_scope_ids=["scope-a"], dry_run=False)
    assert bootstrap["promoted"] >= 5
    before_runs = conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0]
    cases = load_replay_cases(EXPERIENCE_REPLAY_CASES)

    report = build_replay_report(conn, cases=cases, accessible_scope_ids=["scope-a"])

    assert conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == before_runs
    assert report["case_count"] == len(cases) >= 5
    assert report["pass_count"] == report["case_count"]
    assert {case["id"] for case in report["cases"]} >= {
        "core-release-closeout",
        "core-journal-backlog",
        "core-candidate-review",
        "core-vector-rebuild",
        "negative-unrelated-short",
    }


def test_experience_replay_script_is_read_only_and_emits_json_report(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _create_promoted(conn)
    conn.close()

    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps(_case(), ensure_ascii=False) + "\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "experience-replay.py"),
            "--db",
            str(db_path),
            "--case-file",
            str(cases_path),
            "--scope-id",
            "scope-a",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    report = json.loads(result.stdout)
    assert report["case_count"] == 1
    assert report["pass_count"] == 1
    assert report["cases"][0]["playbook_id"] == "pb_acl"

    check_conn = sqlite3.connect(db_path)
    try:
        assert check_conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == 0
    finally:
        check_conn.close()


def test_replay_rejects_empty_and_non_object_case_files(tmp_path):
    empty_path = tmp_path / "empty.json"
    empty_path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ReplayCaseValidationError):
        load_replay_cases(empty_path)
    with pytest.raises(ReplayCaseValidationError):
        build_replay_report(_conn(), cases=[], accessible_scope_ids=["scope-a"])

    invalid_path = tmp_path / "invalid.jsonl"
    invalid_path.write_text(json.dumps(_case(), ensure_ascii=False) + "\n\"not an object\"\n", encoding="utf-8")
    with pytest.raises(ReplayCaseValidationError):
        load_replay_cases(invalid_path)

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "experience-replay.py"),
            "--db",
            str(tmp_path / "missing.sqlite3"),
            "--case-file",
            str(empty_path),
            "--scope-id",
            "scope-a",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_positive_replay_case_fails_without_packet_or_coverage_gain():
    conn = _conn()
    no_packet_case = {
        "id": "no-packet",
        "query": "unmatched task",
        "baseline_text": "rollback negative reachability live nodes",
        "required_terms": ["rollback", "negative reachability", "live nodes"],
    }

    report = build_replay_report(conn, cases=[no_packet_case], accessible_scope_ids=["scope-a"])

    assert report["pass_count"] == 0
    case = report["cases"][0]
    assert case["packet_chars"] == 0
    assert "missing_experience_packet" in case["failures"]
    assert "no_coverage_gain" in case["failures"]


def test_positive_replay_case_requires_non_empty_required_terms():
    conn = _conn()
    _create_promoted(conn)

    report = build_replay_report(
        conn,
        cases=[{"id": "empty-terms", "query": "Need one-way Headscale ACL", "baseline_text": ""}],
        accessible_scope_ids=["scope-a"],
    )

    assert report["pass_count"] == 0
    assert "empty_required_terms" in report["cases"][0]["failures"]


def test_replay_rejects_invalid_required_terms_and_min_coverage_gain():
    conn = _conn()
    _create_promoted(conn)

    for bad_terms in ({"rollback": True}, 123):
        with pytest.raises(ReplayCaseValidationError):
            build_replay_report(
                conn,
                cases=[{"id": "bad-terms", "query": "Need one-way Headscale ACL", "baseline_text": "", "required_terms": bad_terms}],
                accessible_scope_ids=["scope-a"],
            )

    for bad_min_gain in (-0.1, "nan"):
        with pytest.raises(ReplayCaseValidationError):
            build_replay_report(
                conn,
                cases=[
                    {
                        "id": "bad-min-gain",
                        "query": "Need one-way Headscale ACL",
                        "baseline_text": "rollback negative reachability live nodes",
                        "required_terms": ["rollback", "negative reachability", "live nodes"],
                        "min_coverage_gain": bad_min_gain,
                    }
                ],
                accessible_scope_ids=["scope-a"],
            )


def test_replay_term_matching_does_not_count_latin_substrings_as_hits():
    assert coverage_hits("staging deployment only", ["tag"]) == []
    assert coverage_hits("decision only", ["ci"]) == []
    assert coverage_hits("release tag was created and CI passed", ["tag", "ci"]) == ["tag", "ci"]
    assert coverage_hits("发布前必须回读版本号", ["回读版本号"]) == ["回读版本号"]
