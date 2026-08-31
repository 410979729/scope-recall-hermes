"""Tests for dry-run-first candidate review commands."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import scope_recall.cli as cli
from scope_recall.candidate_review import candidate_identity_fields, review_candidate
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "candidate.review.py"


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="candidate-1",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="event-digest",
            target="memory",
            content="Candidate review commands should be dry-run first.",
            metadata=json.dumps(
                {
                    "event_digest": True,
                    "origin_kind": "event_digest",
                    "lifecycle": "candidate",
                    "candidate_status": "needs_review",
                    "review_status": "pending",
                    "automatic_admission": {
                        "source": "event_digest",
                        "route": "memory_review",
                        "reviewed": False,
                    },
                },
                ensure_ascii=False,
            ),
            allow_duplicate=True,
        )
        store_row(
            conn,
            memory_id="memory-keep",
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
            content="Existing promoted memory can supersede a candidate.",
            allow_duplicate=True,
        )
    finally:
        conn.close()
    return db_path


def _metadata(db_path: Path, memory_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return json.loads(row["metadata"])
    finally:
        conn.close()


def test_candidate_identity_fields_whitelist_public_admission_contract():
    fields = candidate_identity_fields(
        source="event-digest",
        metadata={
            "lifecycle": "candidate",
            "automatic_admission": {
                "source": "EVENT_DIGEST",
                "route": "memory_review",
                "reviewed": "false",
                "time_sensitive": "true",
                "reviewed_at": "2026-08-30T12:34:56+00:00",
                "private_operator_path": "C:/private/operator",
                "credentials": {"token": "must-not-escape"},
                "nested": {"arbitrary": ["private"]},
            },
        },
    )

    assert fields["automatic_admission"] == {
        "source": "event_digest",
        "route": "memory_review",
        "reviewed": False,
        "time_sensitive": True,
        "reviewed_at": "2026-08-30T12:34:56+00:00",
    }
    serialized = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    assert "private_operator_path" not in serialized
    assert "must-not-escape" not in serialized
    assert "arbitrary" not in serialized
    poisoned_timestamp = candidate_identity_fields(
        source="event-digest",
        metadata={
            "automatic_admission": {
                "source": "event_digest",
                "route": "memory_review",
                "reviewed": False,
                "reviewed_at": "2026-08-30T12:34:56+00:00 C:/private",
            }
        },
    )
    assert "reviewed_at" not in poisoned_timestamp["automatic_admission"]

    poisoned_identity = candidate_identity_fields(
        source="event-digest",
        metadata={
            "origin_kind": "C:/private/operator",
            "lifecycle": {"private": "must-not-escape"},
            "review_status": "needs review C:/private",
            "event_digest": True,
        },
    )
    assert poisoned_identity["origin_kind"] == "event_digest"
    assert poisoned_identity["lifecycle"] == "active"
    assert poisoned_identity["review_status"] == ""
    poisoned_serialized = json.dumps(
        poisoned_identity, ensure_ascii=False, sort_keys=True
    )
    assert "C:/private/operator" not in poisoned_serialized
    assert "must-not-escape" not in poisoned_serialized
    assert "needs review" not in poisoned_serialized


def _run_review(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_cli_routes_candidate_review_commands_dry_run_first():
    assert cli._match_script_command(["candidates", "promote", "--id", "candidate-1", "--json"]) == (
        "candidate.review.py",
        ["promote", "--dry-run", "--id", "candidate-1", "--json"],
    )
    assert cli._match_script_command(["candidates", "archive", "--id", "candidate-1", "--apply", "--json"]) == (
        "candidate.review.py",
        ["archive", "--id", "candidate-1", "--apply", "--json"],
    )
    assert cli._match_script_command(["candidates", "supersede", "--id", "candidate-1", "--superseded-by", "memory-keep"]) == (
        "candidate.review.py",
        ["supersede", "--dry-run", "--id", "candidate-1", "--superseded-by", "memory-keep"],
    )


def test_candidate_review_promote_defaults_to_dry_run_without_mutation(tmp_path: Path):
    db_path = _make_db(tmp_path)

    result = _run_review("promote", "--db", str(db_path), "--id", "candidate-1", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["after"]["lifecycle"] == "promoted"
    assert payload["after"]["automatic_admission"]["reviewed"] is True
    assert payload["after"]["review_status"] == "promoted"
    assert payload["after"]["origin_kind"] == "event_digest"
    assert _metadata(db_path, "candidate-1").get("candidate_review_action") is None


def test_candidate_review_dry_run_and_apply_never_echo_untrusted_metadata(
    tmp_path: Path,
):
    db_path = _make_db(tmp_path)
    metadata = _metadata(db_path, "candidate-1")
    metadata["private_operator_path"] = "C:/private/operator/MARKER-PATH-771"
    metadata["automatic_admission"].update(
        {
            "credentials": {"token": "MARKER-" + "TOKEN-771"},
            "nested": {"arbitrary": ["MARKER-NESTED-771"]},
        }
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE memories SET metadata=? WHERE id='candidate-1'",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True),),
        )
        conn.commit()
    finally:
        conn.close()

    dry_run = _run_review(
        "promote", "--db", str(db_path), "--id", "candidate-1", "--json"
    )
    applied = _run_review(
        "promote",
        "--db",
        str(db_path),
        "--id",
        "candidate-1",
        "--apply",
        "--json",
    )

    assert dry_run.returncode == 0, dry_run.stderr
    assert applied.returncode == 0, applied.stderr
    for raw in (dry_run.stdout, applied.stdout):
        payload = json.loads(raw)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        assert "MARKER-PATH-771" not in serialized
        assert "MARKER-TOKEN-771" not in serialized
        assert "MARKER-NESTED-771" not in serialized
        assert payload["after"]["automatic_admission"]["source"] == "event_digest"
        assert payload["after"]["automatic_admission"]["reviewed"] is True


def test_candidate_review_apply_archives_and_writes_audit_event(tmp_path: Path):
    db_path = _make_db(tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"vector": {"enabled": True, "backend": "sqlite-bruteforce"}}),
        encoding="utf-8",
    )
    vector = sqlite3.connect(tmp_path / "vector.sqlite3")
    try:
        vector.execute("CREATE TABLE vector_records(id TEXT PRIMARY KEY)")
        vector.execute("INSERT INTO vector_records(id) VALUES ('candidate-1')")
        vector.commit()
    finally:
        vector.close()
    truth = sqlite3.connect(db_path)
    truth.row_factory = sqlite3.Row
    try:
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

    result = _run_review("archive", "--db", str(db_path), "--id", "candidate-1", "--apply", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["applied"] is True
    assert payload["vector_cleanup"] == {
        "status": "queued",
        "executor": "vector_outbox",
        "requested": 1,
        "deleted": 0,
    }
    vector = sqlite3.connect(tmp_path / "vector.sqlite3")
    try:
        # Governance commands must not perform a stale post-commit physical
        # delete. The causal outbox owns companion mutation and may replay later.
        assert vector.execute("SELECT COUNT(*) FROM vector_records WHERE id='candidate-1'").fetchone()[0] == 1
    finally:
        vector.close()
    metadata = _metadata(db_path, "candidate-1")
    assert metadata["lifecycle"] == "archived"
    assert metadata["candidate_review_action"] == "archive"
    assert metadata["automatic_admission"]["reviewed"] is True
    assert metadata["admission_reviewed_at"]
    assert metadata["review_status"] == "archived"
    conn = sqlite3.connect(db_path)
    try:
        audit_count = conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE event_type = 'memory_candidate_review'").fetchone()[0]
        outbox = conn.execute(
            "SELECT operation, status FROM vector_outbox WHERE memory_id='candidate-1' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert audit_count == 1
    assert outbox == ("delete", "pending")


def test_candidate_review_supersede_requires_existing_replacement(tmp_path: Path):
    db_path = _make_db(tmp_path)

    missing = _run_review("supersede", "--db", str(db_path), "--id", "candidate-1", "--superseded-by", "missing", "--json")
    assert missing.returncode == 1
    assert "superseded-by memory not found" in json.loads(missing.stdout)["error"]

    applied = _run_review(
        "supersede",
        "--db",
        str(db_path),
        "--id",
        "candidate-1",
        "--superseded-by",
        "memory-keep",
        "--apply",
        "--json",
    )
    assert applied.returncode == 0, applied.stderr
    metadata = _metadata(db_path, "candidate-1")
    assert metadata["lifecycle"] == "superseded"
    assert metadata["superseded_by"] == "memory-keep"


def test_candidate_review_cas_conflict_preserves_newer_row_and_has_zero_side_effects(tmp_path: Path):
    db_path = _make_db(tmp_path)
    first = sqlite3.connect(db_path)
    second = sqlite3.connect(db_path)
    first.row_factory = sqlite3.Row
    second.row_factory = sqlite3.Row
    try:
        plan = review_candidate(first, memory_id="candidate-1", action="archive", dry_run=True)
        token = plan["version_token"]
        metadata = _metadata(db_path, "candidate-1")
        lifecycle_after_newer_edit = metadata.get("lifecycle")
        metadata["newer_operator_edit"] = True
        second.execute(
            "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = 'candidate-1'",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), "2099-01-01T00:00:00+00:00"),
        )
        second.commit()
        audit_before = second.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0]
        outbox_before = second.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0]

        conflict = review_candidate(
            first,
            memory_id="candidate-1",
            action="archive",
            dry_run=False,
            expected_updated_at=token["updated_at"],
            expected_lifecycle=token["lifecycle"],
        )

        assert conflict["ok"] is False
        assert conflict["status"] == "conflict"
        assert conflict["applied"] is False
        current = json.loads(first.execute("SELECT metadata FROM memories WHERE id = 'candidate-1'").fetchone()[0])
        assert current["newer_operator_edit"] is True
        assert current["lifecycle"] == lifecycle_after_newer_edit
        assert first.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == audit_before
        assert first.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == outbox_before
    finally:
        first.close()
        second.close()


def test_two_candidate_reviewers_only_one_cas_apply_succeeds(tmp_path: Path):
    db_path = _make_db(tmp_path)
    first = sqlite3.connect(db_path)
    second = sqlite3.connect(db_path)
    first.row_factory = sqlite3.Row
    second.row_factory = sqlite3.Row
    try:
        first_plan = review_candidate(first, memory_id="candidate-1", action="promote", dry_run=True)
        second_plan = review_candidate(second, memory_id="candidate-1", action="archive", dry_run=True)
        assert first_plan["version_token"] == second_plan["version_token"]

        winner = review_candidate(
            first,
            memory_id="candidate-1",
            action="promote",
            dry_run=False,
            expected_updated_at=first_plan["version_token"]["updated_at"],
            expected_lifecycle=first_plan["version_token"]["lifecycle"],
        )
        loser = review_candidate(
            second,
            memory_id="candidate-1",
            action="archive",
            dry_run=False,
            expected_updated_at=second_plan["version_token"]["updated_at"],
            expected_lifecycle=second_plan["version_token"]["lifecycle"],
        )

        assert winner["status"] == "applied"
        assert loser["status"] == "conflict"
        current = _metadata(db_path, "candidate-1")
        assert current["lifecycle"] == "promoted"
        assert current["candidate_review_action"] == "promote"
        assert first.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE event_type = 'memory_candidate_review'"
        ).fetchone()[0] == 1
    finally:
        first.close()
        second.close()
