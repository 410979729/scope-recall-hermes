"""Tests for Experience storage operations, feedback, review, merge, and lifecycle metadata.

These cases protect durable playbook auditability and review semantics."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import scope_recall.experience_store as experience_store_module
from scope_recall.experience_models import ExperienceValidationError
from scope_recall.experience_preflight import experience_preflight
from scope_recall.experience_store import (
    create_playbook,
    experience_stats,
    find_duplicate_playbooks,
    inspect_playbook,
    merge_playbooks,
    record_experience_preflight_run,
    record_playbook_feedback,
    review_playbook,
    search_playbooks,
)
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_shared_pool_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_playbooks_cli():
    spec = importlib.util.spec_from_file_location("scope_recall_playbooks_cli", PLUGIN_ROOT / "scripts" / "playbooks.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_operator_cli_routes_playbook_receipt_debt_command():
    import scope_recall.cli as cli

    assert cli._SCRIPT_COMMANDS[("playbooks", "receipts")] == (
        "playbooks.py",
        ["receipts"],
    )


_LEGACY_SCHEMA_VERSION = 10600


def _create_legacy_playbook_db(
    db_path: Path,
    *,
    playbook_ids: tuple[str, ...] = ("pb_old_schema",),
    status: str = "candidate",
) -> None:
    """Create a genuine v1.6-era playbook schema without current migrations.

    Do not replace this fixture with ``ensure_schema()`` plus a lowered
    ``user_version``: the missing temporal-fact tables and migration row are the
    behavior under test.
    """

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                platform TEXT,
                user_id TEXT,
                chat_id TEXT,
                thread_id TEXT,
                gateway_session_key TEXT,
                agent_identity TEXT,
                agent_workspace TEXT,
                session_id TEXT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_recalled_turn INTEGER NOT NULL DEFAULT 0
            );
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                memory_id UNINDEXED,
                content,
                summary
            );
            CREATE TABLE procedural_playbooks (
                id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                shared_scope_id TEXT NOT NULL DEFAULT '',
                task_class TEXT NOT NULL,
                title TEXT NOT NULL,
                trigger TEXT NOT NULL,
                goal TEXT NOT NULL,
                preconditions TEXT NOT NULL DEFAULT '[]',
                steps TEXT NOT NULL DEFAULT '[]',
                pitfalls TEXT NOT NULL DEFAULT '[]',
                verification TEXT NOT NULL DEFAULT '[]',
                cleanup TEXT NOT NULL DEFAULT '[]',
                evidence_anchors TEXT NOT NULL DEFAULT '[]',
                related_skills TEXT NOT NULL DEFAULT '[]',
                environment_constraints TEXT NOT NULL DEFAULT '{}',
                reuse_policy TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'candidate',
                confidence REAL NOT NULL DEFAULT 0.50,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                stale_count INTEGER NOT NULL DEFAULT 0,
                created_from_episode_id TEXT NOT NULL DEFAULT '',
                superseded_by TEXT NOT NULL DEFAULT '',
                last_used_at TEXT,
                last_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE VIRTUAL TABLE procedural_playbooks_fts USING fts5(
                playbook_id UNINDEXED,
                title,
                trigger,
                goal,
                preconditions,
                steps,
                pitfalls,
                verification
            );
            CREATE TABLE playbook_versions (
                id TEXT PRIMARY KEY,
                playbook_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                change_type TEXT NOT NULL,
                change_reason TEXT NOT NULL DEFAULT '',
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                description TEXT NOT NULL,
                checksum TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'applied',
                error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        baseline_description = "Baseline schema ledger for scope-recall v1.6.0"
        checksum_payload = json.dumps(
            {
                "id": "0001_baseline_v1_6_0",
                "plugin_version": "1.6.0",
                "description": baseline_description,
                "schema_version": _LEGACY_SCHEMA_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO schema_migrations(
                id, applied_at, plugin_version, description, checksum, status, error
            ) VALUES ('0001_baseline_v1_6_0', ?, '1.6.0', ?, ?, 'applied', '')
            """,
            (
                "2026-01-01T00:00:00+00:00",
                baseline_description,
                hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest(),
            ),
        )
        for playbook_id in playbook_ids:
            conn.execute(
                """
                INSERT INTO procedural_playbooks(
                    id, scope_id, shared_scope_id, task_class, title, trigger, goal,
                    preconditions, steps, pitfalls, verification, cleanup,
                    evidence_anchors, related_skills, environment_constraints,
                    reuse_policy, status, confidence, created_at, updated_at, metadata
                ) VALUES (?, 'scope-a', '', 'headscale_one_way_acl',
                    'Headscale one-way ACL', 'Temporary legacy fixture',
                    'Apply one-way access safely',
                    '[{"id":"p1","check":"Read live node list.","evidence_required":"headscale output"}]',
                    '[{"number":1,"capability_class":"read_only","action":"Read current ACL policy.","evidence_required":"policy output"}]',
                    '[{"signal":"listing only","mistake":"Assume reachability","correction":"Test connectivity"}]',
                    '["policy validates","positive path works"]',
                    '["Record backup path."]',
                    '[]', '[]', '{}', '{"default_decision":"guided_reuse"}',
                    ?, 0.8, ?, ?, '{}')
                """,
                (
                    playbook_id,
                    status,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO procedural_playbooks_fts(
                    playbook_id, title, trigger, goal, preconditions, steps,
                    pitfalls, verification
                ) VALUES (?, 'Headscale one-way ACL', 'Temporary legacy fixture',
                    'Apply one-way access safely',
                    '[{"id":"p1","check":"Read live node list.","evidence_required":"headscale output"}]',
                    '[{"number":1,"capability_class":"read_only","action":"Read current ACL policy.","evidence_required":"policy output"}]',
                    '[{"signal":"listing only","mistake":"Assume reachability","correction":"Test connectivity"}]',
                    '["policy validates","positive path works"]')
                """,
                (playbook_id,),
            )
            conn.execute(
                """
                INSERT INTO playbook_versions(
                    id, playbook_id, version, change_type, change_reason,
                    snapshot, created_at
                ) VALUES (?, ?, 1, 'create', 'legacy fixture', '{}', ?)
                """,
                (
                    f"pbv_{playbook_id}_1",
                    playbook_id,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            if status == "promoted":
                conn.execute(
                    """
                    INSERT INTO playbook_versions(
                        id, playbook_id, version, change_type, change_reason,
                        snapshot, created_at
                    ) VALUES (?, ?, 2, 'promoted', 'legacy fixture', '{}', ?)
                    """,
                    (
                        f"pbv_{playbook_id}_2",
                        playbook_id,
                        "2026-01-02T00:00:00+00:00",
                    ),
                )
        conn.execute(f"PRAGMA user_version = {_LEGACY_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _database_snapshot(db_path: Path) -> dict[str, object]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return {
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "schema": conn.execute(
                """
                SELECT type, name, tbl_name, COALESCE(sql, '')
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall(),
            "playbooks": conn.execute(
                """
                SELECT id, scope_id, status, superseded_by, updated_at
                FROM procedural_playbooks ORDER BY id
                """
            ).fetchall(),
            "playbook_fts": conn.execute(
                """
                SELECT playbook_id, title, trigger, goal, preconditions, steps,
                       pitfalls, verification
                FROM procedural_playbooks_fts ORDER BY playbook_id
                """
            ).fetchall(),
            "versions": conn.execute(
                """
                SELECT playbook_id, version, change_type, change_reason
                FROM playbook_versions ORDER BY playbook_id, version
                """
            ).fetchall(),
            "migrations": conn.execute(
                "SELECT id, status FROM schema_migrations ORDER BY id"
            ).fetchall(),
        }


def _operator_artifact_snapshot(root: Path) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for directory_name in ("backups", "receipts"):
        directory = root / directory_name
        snapshot[f"{directory_name}_exists"] = directory.exists()
        if directory.exists():
            snapshot[directory_name] = {
                str(path.relative_to(root)): path.read_bytes()
                for path in sorted(directory.rglob("*"))
                if path.is_file()
            }
    return snapshot


def _playbook_apply_args(
    cli,
    db_path: Path,
    *,
    command: str = "promote",
    playbook_id: str = "pb_old_schema",
    superseded_by: str = "",
) -> object:
    argv = [
        command,
        "--db",
        str(db_path),
        "--scope-id",
        "scope-a",
        "--id",
        playbook_id,
        "--reason",
        "reviewed transactional fixture",
        "--apply",
    ]
    if command == "supersede":
        argv.extend(["--superseded-by", superseded_by or "pb_old_schema_target"])
    return cli.parse_args(argv)


def _create_promoted(conn: sqlite3.Connection, *, playbook_id: str, scope_id: str = "scope-a", shared_scope_id: str = "", confidence: float = 0.9) -> None:
    create_playbook(conn, playbook_id=playbook_id, scope_id=scope_id, shared_scope_id=shared_scope_id, payload=_payload(), status="candidate", confidence=confidence)
    review_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=[scope_id, shared_scope_id], action="promote", reason="fixture")


def _payload(*, task_class: str = "headscale_one_way_acl", title: str = "Headscale one-way ACL") -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": task_class,
        "title": title,
        "trigger": "User asks to let management machines access a target while blocking reverse access.",
        "goal": "Apply one-way access safely with live verification.",
        "preconditions": [
            {"id": "p1", "check": "Read live node list.", "evidence_required": "headscale/tailscale output"}
        ],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Read current ACL policy and live node list.",
                "evidence_required": "policy path plus live nodes",
            },
            {
                "number": 2,
                "capability_class": "local_write",
                "action": "Prepare the minimal ACL diff without applying it yet.",
                "evidence_required": "minimal diff",
            },
        ],
        "pitfalls": [
            {"signal": "tailscale status lists nodes", "mistake": "Assume listing equals reachability", "correction": "Check real connectivity."}
        ],
        "verification": ["policy validates", "positive path works", "negative path is blocked"],
        "cleanup": ["Record backup path and verification output."],
        "reuse_policy": {"default_decision": "guided_reuse"},
    }


class _FailingCommitConnection(sqlite3.Connection):
    fail_commit = False

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("injected review commit failure")
        super().commit()


def test_review_playbook_commit_false_requires_caller_owned_transaction():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_commit_owner",
        scope_id="scope-a",
        shared_scope_id="",
        payload=_payload(),
        status="candidate",
    )

    with pytest.raises(ExperienceValidationError, match="caller-owned transaction"):
        review_playbook(
            conn,
            playbook_id="pb_commit_owner",
            accessible_scope_ids=["scope-a"],
            action="promote",
            reason="must remain uncommitted",
            commit=False,
        )

    assert conn.execute(
        "SELECT status FROM procedural_playbooks WHERE id='pb_commit_owner'"
    ).fetchone()[0] == "candidate"


def test_merge_playbooks_commit_false_requires_caller_owned_transaction():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_merge_target",
        scope_id="scope-a",
        payload=_payload(),
        status="candidate",
    )
    create_playbook(
        conn,
        playbook_id="pb_merge_source",
        scope_id="scope-a",
        payload=_payload(),
        status="candidate",
    )
    conn.commit()

    with pytest.raises(ExperienceValidationError, match="caller-owned transaction"):
        merge_playbooks(
            conn,
            target_id="pb_merge_target",
            source_ids=["pb_merge_source"],
            accessible_scope_ids=["scope-a"],
            reason="caller owns commit",
            dry_run=False,
            commit=False,
        )

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT status FROM procedural_playbooks WHERE id='pb_merge_source'"
    ).fetchone()[0] == "candidate"

    conn.execute("BEGIN IMMEDIATE")
    applied = merge_playbooks(
        conn,
        target_id="pb_merge_target",
        source_ids=["pb_merge_source"],
        accessible_scope_ids=["scope-a"],
        reason="caller owns commit",
        dry_run=False,
        commit=False,
    )
    assert applied["merged"] is True
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute(
        "SELECT status FROM procedural_playbooks WHERE id='pb_merge_source'"
    ).fetchone()[0] == "candidate"


def test_review_playbook_commit_failure_rolls_back_without_masking_error():
    conn = sqlite3.connect(":memory:", factory=_FailingCommitConnection)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    create_playbook(
        conn,
        playbook_id="pb_commit_failure",
        scope_id="scope-a",
        shared_scope_id="",
        payload=_payload(),
        status="candidate",
    )
    before_versions = conn.execute(
        "SELECT COUNT(*) FROM playbook_versions WHERE playbook_id='pb_commit_failure'"
    ).fetchone()[0]
    conn.fail_commit = True

    with pytest.raises(sqlite3.OperationalError, match="injected review commit failure"):
        review_playbook(
            conn,
            playbook_id="pb_commit_failure",
            accessible_scope_ids=["scope-a"],
            action="promote",
            reason="fault injection",
        )

    conn.fail_commit = False
    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT status FROM procedural_playbooks WHERE id='pb_commit_failure'"
    ).fetchone()[0] == "candidate"
    assert conn.execute(
        "SELECT COUNT(*) FROM playbook_versions WHERE playbook_id='pb_commit_failure'"
    ).fetchone()[0] == before_versions


def test_review_playbook_fts_trigger_failure_rolls_back_status_and_version():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_fts_failure",
        scope_id="scope-a",
        shared_scope_id="",
        payload=_payload(),
        status="candidate",
    )
    conn.executescript(
        """
        CREATE TRIGGER fail_playbook_fts_after_update
        AFTER UPDATE ON procedural_playbooks
        BEGIN
            SELECT RAISE(ABORT, 'injected procedural_playbooks_fts failure');
        END;
        """
    )
    conn.commit()
    before_versions = conn.execute(
        "SELECT COUNT(*) FROM playbook_versions WHERE playbook_id='pb_fts_failure'"
    ).fetchone()[0]

    with pytest.raises(sqlite3.DatabaseError, match="procedural_playbooks_fts"):
        review_playbook(
            conn,
            playbook_id="pb_fts_failure",
            accessible_scope_ids=["scope-a"],
            action="promote",
            reason="fault injection",
        )

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT status FROM procedural_playbooks WHERE id='pb_fts_failure'"
    ).fetchone()[0] == "candidate"
    assert conn.execute(
        "SELECT COUNT(*) FROM playbook_versions WHERE playbook_id='pb_fts_failure'"
    ).fetchone()[0] == before_versions


def test_create_search_inspect_playbook_with_fts_and_scope_filtering():
    conn = _conn()
    created = create_playbook(
        conn,
        playbook_id="pb_acl",
        scope_id="scope-a",
        shared_scope_id="shared-a",
        payload=_payload(),
        status="candidate",
        confidence=0.61,
        created_from_episode_id="episode-1",
        metadata={"source": "unit-test"},
    )

    assert created["id"] == "pb_acl"
    assert created["status"] == "candidate"
    assert created["task_class"] == "headscale_one_way_acl"
    assert created["requires_operator_review"] is True

    visible = search_playbooks(conn, query="one-way ACL management access", accessible_scope_ids=["scope-a"], limit=5)
    hidden = search_playbooks(conn, query="one-way ACL management access", accessible_scope_ids=["scope-b"], limit=5)

    assert [item["id"] for item in visible] == ["pb_acl"]
    assert hidden == []
    assert visible[0]["match_source"] == "fts"
    assert visible[0]["steps"][0]["capability_class"] == "read_only"

    inspected = inspect_playbook(conn, playbook_id="pb_acl", accessible_scope_ids=["scope-a"])
    assert inspected["found"] is True
    assert inspected["playbook"]["title"] == "Headscale one-way ACL"
    assert inspected["versions"][0]["version"] == 1
    assert inspected["versions"][0]["change_type"] == "create"

    assert inspect_playbook(conn, playbook_id="pb_acl", accessible_scope_ids=["scope-b"])["found"] is False


def test_search_uses_fts_index_instead_of_labeling_python_scan_as_fts():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_fts")

    assert search_playbooks(conn, query="management access", accessible_scope_ids=["scope-a"], limit=5)[0]["match_source"] == "fts"
    conn.execute("DELETE FROM procedural_playbooks_fts WHERE playbook_id = ?", ("pb_fts",))
    conn.commit()

    assert search_playbooks(conn, query="management access", accessible_scope_ids=["scope-a"], limit=5) == []


def test_review_and_feedback_update_status_counts_and_stats():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_acl", scope_id="scope-a", shared_scope_id="", payload=_payload(), confidence=0.9)

    reviewed = review_playbook(conn, playbook_id="pb_acl", accessible_scope_ids=["scope-a"], action="promote", reason="manual review passed")
    assert reviewed["reviewed"] is True
    assert reviewed["status"] == "promoted"
    assert reviewed["version"] == 2

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_acl",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        decision="guided_reuse",
        evidence=["policy check passed"],
        preconditions_checked=[{"id": "p1", "status": "passed", "evidence": "live node list"}],
        steps_completed=[{"number": 1, "status": "done", "evidence": "policy read"}],
        outcome_reason="fixture success",
    )
    assert feedback["recorded"] is True
    assert feedback["success_count"] == 1
    assert feedback["failure_count"] == 0
    assert feedback["status"] == "promoted"
    assert feedback["confidence"] >= 0.82
    verified = conn.execute("SELECT last_used_at, last_verified_at FROM procedural_playbooks WHERE id = ?", ("pb_acl",)).fetchone()
    assert verified["last_used_at"]
    assert verified["last_verified_at"] == verified["last_used_at"]
    run = conn.execute("SELECT preconditions_checked, steps_completed, evidence FROM experience_runs WHERE outcome = 'success'").fetchone()
    assert run is not None
    assert run["evidence"] == '["policy check passed"]'
    assert "live node list" in run["preconditions_checked"]
    assert "policy read" in run["steps_completed"]

    failed = record_playbook_feedback(
        conn,
        playbook_id="pb_acl",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        decision="direct_reuse",
        evidence=["negative check failed"],
        outcome_reason="fixture failure",
    )
    assert failed["failure_count"] == 1
    assert failed["status"] == "needs_review"
    assert failed["reflection_recorded"] is True
    reflection = conn.execute("SELECT playbook_id, event_type, outcome, evidence, corrections FROM reflection_events WHERE playbook_id = ?", ("pb_acl",)).fetchone()
    assert reflection is not None
    assert reflection["event_type"] == "reuse_feedback"
    assert reflection["outcome"] == "failed"
    assert "negative check failed" in reflection["evidence"]
    assert "fixture failure" in reflection["corrections"]

    stats = experience_stats(conn, accessible_scope_ids=["scope-a"])
    assert stats["playbooks"]["total"] == 1
    assert stats["playbooks"]["by_status"]["needs_review"] == 1
    assert stats["runs"]["total"] == 2
    assert stats["runs"]["by_outcome"] == {"failed": 1, "success": 1}


def test_negative_feedback_threshold_delays_needs_review_until_limit():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_threshold")

    first = record_playbook_feedback(
        conn,
        playbook_id="pb_threshold",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        decision="guided_reuse",
        evidence=["skill smoke failed once"],
        outcome_reason="first skill failure",
        negative_feedback_threshold=2,
    )
    row_after_first = conn.execute("SELECT status, failure_count FROM procedural_playbooks WHERE id = ?", ("pb_threshold",)).fetchone()

    assert first["recorded"] is True
    assert first["negative_feedback_threshold"] == 2
    assert first["negative_feedback_count"] == 1
    assert first["status"] == "promoted"
    assert row_after_first["status"] == "promoted"
    assert row_after_first["failure_count"] == 1

    second = record_playbook_feedback(
        conn,
        playbook_id="pb_threshold",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        decision="guided_reuse",
        evidence=["skill smoke failed again"],
        outcome_reason="second skill failure",
        negative_feedback_threshold=2,
    )

    assert second["negative_feedback_count"] == 2
    assert second["status"] == "needs_review"


def test_unknown_feedback_records_run_without_changing_confidence_or_counts():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_unknown", confidence=0.9)
    before = conn.execute("SELECT confidence, success_count, failure_count, stale_count, status FROM procedural_playbooks WHERE id = ?", ("pb_unknown",)).fetchone()

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_unknown",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="unknown",
        decision="guided_reuse",
        evidence=["preflight run awaiting outcome"],
    )
    after = conn.execute("SELECT confidence, success_count, failure_count, stale_count, status FROM procedural_playbooks WHERE id = ?", ("pb_unknown",)).fetchone()

    assert feedback["recorded"] is True
    assert feedback["global_updated"] is False
    assert feedback["status"] == before["status"] == after["status"]
    assert after["confidence"] == before["confidence"]
    assert after["success_count"] == before["success_count"] == 0
    assert after["failure_count"] == before["failure_count"] == 0
    assert after["stale_count"] == before["stale_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM experience_runs WHERE playbook_id = ? AND outcome = 'unknown'", ("pb_unknown",)).fetchone()[0] == 1


def _record_pending_preflight_run(
    conn: sqlite3.Connection,
    *,
    playbook_id: str,
    scope_id: str = "scope-a",
    accessible_scope_ids: list[str] | None = None,
    decision: str = "guided_reuse",
) -> str:
    """Create one pending experience_runs row through the existing preflight recorder."""

    inspected = inspect_playbook(
        conn,
        playbook_id=playbook_id,
        accessible_scope_ids=accessible_scope_ids or [scope_id],
    )
    playbook = inspected["playbook"] if isinstance(inspected.get("playbook"), dict) else {}
    receipt = record_experience_preflight_run(
        conn,
        playbook=playbook,
        scope_id=scope_id,
        decision=decision,
        query="Need one-way Headscale ACL so management can access target",
        reasons=["fixture pending run"],
    )
    run_id = str(receipt.get("run_id") or "")
    assert receipt.get("recorded") is True
    assert run_id.startswith("xrun_")
    return run_id


def test_feedback_with_run_id_finalizes_pending_preflight_run_in_place():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_close")
    run_id = _record_pending_preflight_run(conn, playbook_id="pb_close")
    pending = conn.execute(
        "SELECT started_at, finished_at FROM experience_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_close",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        run_id=run_id,
        evidence=["live check passed"],
        outcome_reason="closed from pending preflight",
    )
    row = conn.execute("SELECT * FROM experience_runs WHERE id = ?", (run_id,)).fetchone()
    counts = conn.execute(
        "SELECT success_count, failure_count FROM procedural_playbooks WHERE id = ?",
        ("pb_close",),
    ).fetchone()

    assert feedback["recorded"] is True
    assert feedback["run_id"] == run_id
    assert feedback["success_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == 1
    assert row["outcome"] == "success"
    assert row["started_at"] == pending["started_at"]
    assert row["finished_at"]
    assert row["outcome_reason"] == "closed from pending preflight"
    assert counts["success_count"] == 1
    assert counts["failure_count"] == 0


def test_minimal_feedback_preserves_pending_preflight_evidence():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_preserve_all")
    run_id = _record_pending_preflight_run(conn, playbook_id="pb_preserve_all")
    before = conn.execute(
        "SELECT evidence, preconditions_checked, steps_completed FROM experience_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_preserve_all",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        run_id=run_id,
    )
    after = conn.execute(
        "SELECT evidence, preconditions_checked, steps_completed FROM experience_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    assert feedback["recorded"] is True
    assert dict(after) == dict(before)


def test_minimal_feedback_preserves_pending_preflight_decision():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_preserve_decision")
    run_id = _record_pending_preflight_run(
        conn,
        playbook_id="pb_preserve_decision",
        decision="direct_reuse",
    )

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_preserve_decision",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        run_id=run_id,
    )
    finalized = conn.execute(
        "SELECT decision FROM experience_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    assert feedback["recorded"] is True
    assert finalized["decision"] == "direct_reuse"


def test_partial_feedback_only_replaces_fields_explicitly_supplied():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_preserve_partial")
    run_id = _record_pending_preflight_run(conn, playbook_id="pb_preserve_partial")
    before = conn.execute(
        "SELECT evidence, preconditions_checked, steps_completed FROM experience_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_preserve_partial",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        run_id=run_id,
        evidence=["explicit completion evidence"],
    )
    after = conn.execute(
        "SELECT evidence, preconditions_checked, steps_completed FROM experience_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    assert feedback["recorded"] is True
    assert json.loads(after["evidence"]) == ["explicit completion evidence"]
    assert after["preconditions_checked"] == before["preconditions_checked"]
    assert after["steps_completed"] == before["steps_completed"]


def test_feedback_with_run_id_does_not_recount_already_finalized_run():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_closed_once")
    run_id = _record_pending_preflight_run(conn, playbook_id="pb_closed_once")
    first = record_playbook_feedback(
        conn,
        playbook_id="pb_closed_once",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        run_id=run_id,
        evidence=["first close"],
    )
    second = record_playbook_feedback(
        conn,
        playbook_id="pb_closed_once",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        run_id=run_id,
        evidence=["must not recount"],
    )
    row = conn.execute("SELECT outcome, started_at FROM experience_runs WHERE id = ?", (run_id,)).fetchone()
    counts = conn.execute(
        "SELECT success_count, failure_count FROM procedural_playbooks WHERE id = ?",
        ("pb_closed_once",),
    ).fetchone()

    assert first["recorded"] is True
    assert first["success_count"] == 1
    assert second == {
        "recorded": False,
        "id": "pb_closed_once",
        "run_id": run_id,
        "error": "run_already_finalized",
        "outcome": "success",
    }
    assert conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == 1
    assert row["outcome"] == "success"
    assert counts["success_count"] == 1
    assert counts["failure_count"] == 0


def test_feedback_with_run_id_rejects_unknown_outcome_without_mutating():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_unknown_close")
    run_id = _record_pending_preflight_run(conn, playbook_id="pb_unknown_close")

    blocked = record_playbook_feedback(
        conn,
        playbook_id="pb_unknown_close",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="unknown",
        run_id=run_id,
        evidence=["still pending"],
    )
    row = conn.execute("SELECT outcome FROM experience_runs WHERE id = ?", (run_id,)).fetchone()
    counts = conn.execute(
        "SELECT success_count FROM procedural_playbooks WHERE id = ?",
        ("pb_unknown_close",),
    ).fetchone()

    assert blocked == {
        "recorded": False,
        "id": "pb_unknown_close",
        "run_id": run_id,
        "error": "outcome_not_terminal",
    }
    assert row["outcome"] == "unknown"
    assert counts["success_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == 1


def test_feedback_with_run_id_requires_matching_playbook_and_accessible_scope():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_owner")
    _create_promoted(conn, playbook_id="pb_other")
    owner_run = _record_pending_preflight_run(conn, playbook_id="pb_owner")

    mismatch = record_playbook_feedback(
        conn,
        playbook_id="pb_other",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        run_id=owner_run,
        evidence=["wrong playbook"],
    )
    missing = record_playbook_feedback(
        conn,
        playbook_id="pb_owner",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        run_id="xrun_missing",
        evidence=["missing run"],
    )

    create_playbook(
        conn,
        playbook_id="pb_shared_run",
        scope_id="scope-owner",
        shared_scope_id="pool",
        payload=_payload(),
        status="candidate",
        confidence=0.9,
    )
    review_playbook(
        conn,
        playbook_id="pb_shared_run",
        accessible_scope_ids=["scope-owner", "pool"],
        action="promote",
        reason="fixture",
    )
    shared_run = _record_pending_preflight_run(
        conn,
        playbook_id="pb_shared_run",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a", "pool"],
    )
    hidden = record_playbook_feedback(
        conn,
        playbook_id="pb_shared_run",
        scope_id="scope-b",
        accessible_scope_ids=["scope-b", "pool"],
        outcome="failed",
        run_id=shared_run,
        evidence=["other consumer cannot close this run"],
    )

    assert mismatch == {"recorded": False, "id": "pb_other", "error": "run_playbook_mismatch"}
    assert missing == {"recorded": False, "id": "pb_owner", "error": "run_not_found"}
    assert hidden == {"recorded": False, "id": "pb_shared_run", "error": "run_not_found"}
    assert json.dumps([mismatch, missing, hidden], ensure_ascii=False).count("error") == 3
    assert conn.execute("SELECT outcome FROM experience_runs WHERE id = ?", (owner_run,)).fetchone()[0] == "unknown"
    assert conn.execute("SELECT outcome FROM experience_runs WHERE id = ?", (shared_run,)).fetchone()[0] == "unknown"
    assert conn.execute("SELECT success_count FROM procedural_playbooks WHERE id = ?", ("pb_owner",)).fetchone()[0] == 0
    assert conn.execute("SELECT success_count FROM procedural_playbooks WHERE id = ?", ("pb_other",)).fetchone()[0] == 0
    assert conn.execute("SELECT failure_count FROM procedural_playbooks WHERE id = ?", ("pb_shared_run",)).fetchone()[0] == 0


def test_feedback_without_run_id_still_inserts_independent_run():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_compat")
    pending_id = _record_pending_preflight_run(conn, playbook_id="pb_compat")

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_compat",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        evidence=["legacy feedback without run_id"],
    )
    rows = conn.execute(
        "SELECT id, outcome FROM experience_runs WHERE playbook_id = ? ORDER BY started_at, id",
        ("pb_compat",),
    ).fetchall()

    assert feedback["recorded"] is True
    assert feedback["success_count"] == 1
    assert feedback["run_id"] in {row["id"] for row in rows}
    assert feedback["run_id"] != pending_id
    assert len(rows) == 2
    assert {row["id"] for row in rows} != {pending_id}
    assert pending_id in {row["id"] for row in rows}
    assert {row["outcome"] for row in rows} == {"unknown", "success"}


def test_feedback_rejects_secret_like_run_id_before_persisting():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_run_secret")
    run_id = _record_pending_preflight_run(conn, playbook_id="pb_run_secret")

    with pytest.raises(ExperienceValidationError):
        record_playbook_feedback(
            conn,
            playbook_id="pb_run_secret",
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="success",
            run_id="token=not_a_real_key_12345",
            evidence=["safe"],
        )

    assert conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0] == 1
    assert conn.execute("SELECT outcome FROM experience_runs WHERE id = ?", (run_id,)).fetchone()[0] == "unknown"


def test_find_duplicate_playbooks_groups_same_task_class_and_title():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_release_a", scope_id="scope-a", shared_scope_id="", payload=_payload(task_class="scope_recall_release_closeout", title="scope-recall：发布收口"), confidence=0.72)
    create_playbook(conn, playbook_id="pb_release_b", scope_id="scope-a", shared_scope_id="", payload=_payload(task_class="scope_recall_release_closeout", title="scope-recall：发布收口"), confidence=0.88)
    create_playbook(conn, playbook_id="pb_other", scope_id="scope-a", shared_scope_id="", payload=_payload(task_class="scope_recall_docs_quality", title="scope-recall：文档质量检查"), confidence=0.9)

    groups = find_duplicate_playbooks(conn, accessible_scope_ids=["scope-a"])

    assert len(groups) == 1
    group = groups[0]
    assert group["task_class"] == "scope_recall_release_closeout"
    assert group["title"] == "scope-recall：发布收口"
    assert group["canonical_id"] == "pb_release_b"
    assert [item["id"] for item in group["items"]] == ["pb_release_b", "pb_release_a"]


def test_merge_playbooks_supersedes_sources_and_writes_versions():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", shared_scope_id="", payload=_payload(task_class="scope_recall_release_closeout", title="scope-recall：发布收口"), confidence=0.9)
    create_playbook(conn, playbook_id="pb_source", scope_id="scope-a", shared_scope_id="", payload=_payload(task_class="scope_recall_release_closeout", title="scope-recall：发布收口"), confidence=0.7)
    before_changes = conn.total_changes

    dry = merge_playbooks(conn, target_id="pb_target", source_ids=["pb_source"], accessible_scope_ids=["scope-a"], reason="dedupe fixture", dry_run=True)
    assert dry["merged"] is False
    assert dry["dry_run"] is True
    assert dry["source_ids"] == ["pb_source"]
    assert conn.total_changes == before_changes

    applied = merge_playbooks(conn, target_id="pb_target", source_ids=["pb_source"], accessible_scope_ids=["scope-a"], reason="dedupe fixture", dry_run=False)

    assert applied["merged"] is True
    assert applied["target_id"] == "pb_target"
    source = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()
    target = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_target'").fetchone()
    assert source["status"] == "superseded"
    assert source["superseded_by"] == "pb_target"
    assert target["status"] == "candidate"
    assert target["superseded_by"] == ""
    version_rows = conn.execute("SELECT playbook_id, change_type, change_reason FROM playbook_versions ORDER BY created_at, version").fetchall()
    version_payload = [(row["playbook_id"], row["change_type"], row["change_reason"]) for row in version_rows]
    assert ("pb_source", "superseded", "dedupe fixture") in version_payload
    assert ("pb_target", "merge", "dedupe fixture") in version_payload
    version_count_after_first_apply = conn.execute("SELECT COUNT(*) FROM playbook_versions").fetchone()[0]
    timestamps_after_first_apply = {
        row["id"]: row["updated_at"]
        for row in conn.execute("SELECT id, updated_at FROM procedural_playbooks WHERE id IN ('pb_source', 'pb_target')").fetchall()
    }

    repeated = merge_playbooks(conn, target_id="pb_target", source_ids=["pb_source"], accessible_scope_ids=["scope-a"], reason="dedupe fixture", dry_run=False)

    assert repeated["merged"] is True
    assert repeated["changed"] is False
    assert repeated["idempotent"] is True
    assert repeated["idempotent_source_ids"] == ["pb_source"]
    assert repeated["changed_source_ids"] == []
    assert conn.execute("SELECT COUNT(*) FROM playbook_versions").fetchone()[0] == version_count_after_first_apply
    timestamps_after_second_apply = {
        row["id"]: row["updated_at"]
        for row in conn.execute("SELECT id, updated_at FROM procedural_playbooks WHERE id IN ('pb_source', 'pb_target')").fetchall()
    }
    assert timestamps_after_second_apply == timestamps_after_first_apply


def test_merge_playbooks_rejects_cross_scope_and_self_merge():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", shared_scope_id="", payload=_payload(), confidence=0.9)
    create_playbook(conn, playbook_id="pb_hidden", scope_id="scope-b", shared_scope_id="", payload=_payload(), confidence=0.7)

    hidden = merge_playbooks(conn, target_id="pb_target", source_ids=["pb_hidden"], accessible_scope_ids=["scope-a"], reason="blocked", dry_run=False)
    self_merge = merge_playbooks(conn, target_id="pb_target", source_ids=["pb_target"], accessible_scope_ids=["scope-a"], reason="blocked", dry_run=False)

    assert hidden == {"merged": False, "dry_run": False, "target_id": "pb_target", "error": "source_not_found", "missing_source_ids": ["pb_hidden"]}
    assert self_merge["error"] == "self_merge"
    assert conn.execute("SELECT status FROM procedural_playbooks WHERE id = 'pb_hidden'").fetchone()[0] == "candidate"


def test_merge_playbooks_rejects_semantic_mismatch_without_force():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", payload=_payload(task_class="journal_backlog_drain", title="scope-recall：journal backlog 清理"), confidence=0.9)
    create_playbook(conn, playbook_id="pb_source", scope_id="scope-a", payload=_payload(task_class="github_release_publish", title="GitHub：release 发布核验"), confidence=0.7)
    before_changes = conn.total_changes

    blocked = merge_playbooks(conn, target_id="pb_target", source_ids=["pb_source"], accessible_scope_ids=["scope-a"], reason="wrong id", dry_run=False)

    assert blocked["error"] == "semantic_mismatch"
    assert blocked["mismatches"][0]["source_id"] == "pb_source"
    assert conn.total_changes == before_changes

    forced = merge_playbooks(conn, target_id="pb_target", source_ids=["pb_source"], accessible_scope_ids=["scope-a"], reason="operator force merge", dry_run=False, force_cross_class=True)

    assert forced["merged"] is True
    assert conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()["superseded_by"] == "pb_target"


def test_find_duplicate_playbooks_splits_unrelated_scope_title_collisions():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_owner", scope_id="scope-a", shared_scope_id="", payload=_payload(), confidence=0.9)
    create_playbook(conn, playbook_id="pb_session", scope_id="scope-a-session", shared_scope_id="scope-a", payload=_payload(), confidence=0.7)
    create_playbook(conn, playbook_id="pb_other", scope_id="scope-b", shared_scope_id="", payload=_payload(), confidence=0.8)

    groups = find_duplicate_playbooks(conn, accessible_scope_ids=["scope-a", "scope-a-session", "scope-b"], limit=10)

    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert {item["id"] for item in groups[0]["items"]} == {"pb_owner", "pb_session"}


def test_review_supersede_requires_existing_same_owner_canonical():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_source", scope_id="scope-a", shared_scope_id="", payload=_payload(), confidence=0.7)
    create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", shared_scope_id="", payload=_payload(), confidence=0.9)
    create_playbook(conn, playbook_id="pb_cross_scope", scope_id="scope-b", shared_scope_id="", payload=_payload(), confidence=0.9)
    before_versions = conn.execute("SELECT COUNT(*) FROM playbook_versions WHERE playbook_id = 'pb_source'").fetchone()[0]

    empty = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="")
    missing = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_missing")
    cross_scope = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a", "scope-b"], action="supersede", superseded_by="pb_cross_scope")
    self_supersede = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_source")

    row = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()
    assert empty == {"reviewed": False, "dry_run": False, "changed": False, "id": "pb_source", "error": "superseded_by_required"}
    assert missing == {"reviewed": False, "dry_run": False, "changed": False, "id": "pb_source", "error": "superseded_by_not_found", "superseded_by": "pb_missing"}
    assert cross_scope == {"reviewed": False, "dry_run": False, "changed": False, "id": "pb_source", "error": "superseded_by_scope_mismatch", "superseded_by": "pb_cross_scope"}
    assert self_supersede == {"reviewed": False, "dry_run": False, "changed": False, "id": "pb_source", "error": "self_supersede"}
    assert row["status"] == "candidate"
    assert row["superseded_by"] == ""
    assert conn.execute("SELECT COUNT(*) FROM playbook_versions WHERE playbook_id = 'pb_source'").fetchone()[0] == before_versions

    applied = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_target", reason="dedupe fixture")

    assert applied["reviewed"] is True
    assert applied["status"] == "superseded"
    assert applied["superseded_by"] == "pb_target"
    row = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()
    assert row["status"] == "superseded"
    assert row["superseded_by"] == "pb_target"


def test_review_supersede_accepts_explicit_runtime_owner_alias_group_only():
    conn = _conn()
    legacy_scope = "platform:telegram|workspace:hermes|agent:default|user:9000000001"  # fixture
    canonical_scope = "workspace:hermes|agent:default|canonical_user:joy"
    unrelated_scope = "workspace:hermes|agent:default|canonical_user:other"
    create_playbook(
        conn,
        playbook_id="pb_legacy_core",
        scope_id=legacy_scope,
        payload=_payload(),
        confidence=0.9,
    )
    create_playbook(
        conn,
        playbook_id="pb_canonical_candidate",
        scope_id=canonical_scope,
        payload=_payload(),
        confidence=0.7,
    )
    create_playbook(
        conn,
        playbook_id="pb_unrelated",
        scope_id=unrelated_scope,
        payload=_payload(),
        confidence=0.7,
    )
    accessible = [legacy_scope, canonical_scope, unrelated_scope]

    exact_groups = find_duplicate_playbooks(
        conn,
        accessible_scope_ids=accessible,
        limit=10,
    )
    alias_groups = find_duplicate_playbooks(
        conn,
        accessible_scope_ids=accessible,
        owner_scope_aliases=[legacy_scope, canonical_scope],
        limit=10,
    )
    blocked = review_playbook(
        conn,
        playbook_id="pb_canonical_candidate",
        accessible_scope_ids=accessible,
        action="supersede",
        superseded_by="pb_legacy_core",
        reason="identity migration dedupe",
        dry_run=True,
    )
    allowed = review_playbook(
        conn,
        playbook_id="pb_canonical_candidate",
        accessible_scope_ids=accessible,
        owner_scope_aliases=[legacy_scope, canonical_scope],
        action="supersede",
        superseded_by="pb_legacy_core",
        reason="identity migration dedupe",
        dry_run=True,
    )
    unrelated = review_playbook(
        conn,
        playbook_id="pb_unrelated",
        accessible_scope_ids=accessible,
        owner_scope_aliases=[legacy_scope, canonical_scope],
        action="supersede",
        superseded_by="pb_legacy_core",
        reason="must remain isolated",
        dry_run=True,
    )
    forged_alias = review_playbook(
        conn,
        playbook_id="pb_canonical_candidate",
        accessible_scope_ids=accessible,
        owner_scope_aliases=[legacy_scope, canonical_scope, "forged-owner"],
        action="supersede",
        superseded_by="pb_legacy_core",
        reason="must fail closed",
        dry_run=True,
    )

    assert exact_groups == []
    assert len(alias_groups) == 1
    assert {item["id"] for item in alias_groups[0]["items"]} == {
        "pb_legacy_core",
        "pb_canonical_candidate",
    }
    assert blocked["error"] == "superseded_by_scope_mismatch"
    assert allowed["changed"] is True
    assert allowed["status"] == "superseded"
    assert unrelated["error"] == "superseded_by_scope_mismatch"
    assert forged_alias["error"] == "owner_scope_alias_not_accessible"


def test_review_validation_token_binds_runtime_owner_alias_group():
    conn = _conn()
    legacy_scope = "legacy-owner"
    canonical_scope = "canonical-owner"
    create_playbook(conn, playbook_id="pb_legacy", scope_id=legacy_scope, payload=_payload(), confidence=0.9)
    create_playbook(conn, playbook_id="pb_candidate", scope_id=canonical_scope, payload=_payload(), confidence=0.7)
    accessible = [legacy_scope, canonical_scope, "another-accessible-owner"]
    aliases = [legacy_scope, canonical_scope]
    dry_run = review_playbook(
        conn,
        playbook_id="pb_candidate",
        accessible_scope_ids=accessible,
        owner_scope_aliases=aliases,
        action="supersede",
        superseded_by="pb_legacy",
        reason="identity migration dedupe",
        dry_run=True,
    )

    stale = review_playbook(
        conn,
        playbook_id="pb_candidate",
        accessible_scope_ids=accessible,
        owner_scope_aliases=[legacy_scope, canonical_scope, "another-accessible-owner"],
        action="supersede",
        superseded_by="pb_legacy",
        reason="identity migration dedupe",
        validated_payload=dry_run,
    )
    applied = review_playbook(
        conn,
        playbook_id="pb_candidate",
        accessible_scope_ids=accessible,
        owner_scope_aliases=aliases,
        action="supersede",
        superseded_by="pb_legacy",
        reason="identity migration dedupe",
        validated_payload=dry_run,
    )

    assert stale["error"] == "stale_validation"
    assert applied["reviewed"] is True
    assert applied["status"] == "superseded"


def test_review_playbook_is_idempotent_for_repeated_supersede():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_source", scope_id="scope-a", payload=_payload(), confidence=0.7)
    create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", payload=_payload(), confidence=0.9)

    first = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_target", reason="dedupe fixture")
    before = conn.execute("SELECT status, superseded_by, updated_at FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()
    before_versions = conn.execute("SELECT COUNT(*) FROM playbook_versions WHERE playbook_id = 'pb_source'").fetchone()[0]
    second = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_target", reason="dedupe fixture retry")
    after = conn.execute("SELECT status, superseded_by, updated_at FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()

    assert first["changed"] is True
    assert second["reviewed"] is True
    assert second["changed"] is False
    assert second["idempotent"] is True
    assert second["superseded_by"] == "pb_target"
    assert dict(after) == dict(before)
    assert conn.execute("SELECT COUNT(*) FROM playbook_versions WHERE playbook_id = 'pb_source'").fetchone()[0] == before_versions


def test_review_supersede_rejects_semantic_mismatch_unless_forced():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_source", scope_id="scope-a", payload=_payload(task_class="github_release_publish", title="GitHub：release 发布核验"), confidence=0.7)
    create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", payload=_payload(task_class="journal_backlog_drain", title="scope-recall：journal backlog 清理"), confidence=0.9)
    before_versions = conn.execute("SELECT COUNT(*) FROM playbook_versions WHERE playbook_id = 'pb_source'").fetchone()[0]

    blocked = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_target", reason="wrong id")
    forced_without_reason = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_target", force_cross_class=True)
    forced = review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="supersede", superseded_by="pb_target", reason="operator intentionally merges renamed playbook", force_cross_class=True)

    assert blocked["error"] == "semantic_mismatch"
    assert blocked["changed"] is False
    assert forced_without_reason["error"] == "force_reason_required"
    assert forced_without_reason["changed"] is False
    assert forced["reviewed"] is True
    assert forced["changed"] is True
    assert forced["superseded_by"] == "pb_target"
    assert conn.execute("SELECT COUNT(*) FROM playbook_versions WHERE playbook_id = 'pb_source'").fetchone()[0] == before_versions + 1


def test_review_supersede_allows_owner_scope_shared_scope_overlap():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_source", scope_id="scope-a-session", shared_scope_id="scope-a", payload=_payload(), confidence=0.7)
    create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", shared_scope_id="scope-a-session", payload=_payload(), confidence=0.9)

    applied = review_playbook(
        conn,
        playbook_id="pb_source",
        accessible_scope_ids=["scope-a", "scope-a-session"],
        action="supersede",
        superseded_by="pb_target",
        reason="dedupe fixture across owner/shared scope boundary",
    )

    assert applied["reviewed"] is True
    assert applied["status"] == "superseded"
    assert applied["superseded_by"] == "pb_target"
    row = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()
    assert row["status"] == "superseded"
    assert row["superseded_by"] == "pb_target"


def test_playbooks_cli_supersede_routes_to_review_playbook(tmp_path):
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        create_playbook(conn, playbook_id="pb_source", scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.7)
        review_playbook(conn, playbook_id="pb_source", accessible_scope_ids=["scope-a"], action="needs_review", reason="fixture candidate review")
        create_playbook(conn, playbook_id="pb_target", scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.9)
        review_playbook(conn, playbook_id="pb_target", accessible_scope_ids=["scope-a"], action="promote", reason="fixture canonical")
    finally:
        conn.close()

    dry_args = cli.parse_args(
        [
            "supersede",
            "--db",
            str(db_path),
            "--scope-id",
            "scope-a",
            "--id",
            "pb_source",
            "--superseded-by",
            "pb_target",
            "--reason",
            "duplicate group closeout",
        ]
    )

    dry_payload = cli.build_payload(dry_args)

    assert dry_payload["ok"] is True
    assert dry_payload["dry_run"] is True
    assert dry_payload["changed"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()
        assert row["status"] == "needs_review"
        assert row["superseded_by"] == ""
    finally:
        conn.close()

    args = cli.parse_args(
        [
            "supersede",
            "--db",
            str(db_path),
            "--scope-id",
            "scope-a",
            "--id",
            "pb_source",
            "--superseded-by",
            "pb_target",
            "--reason",
            "duplicate group closeout",
            "--apply",
        ]
    )

    payload = cli.build_payload(args)

    assert payload["ok"] is True
    assert payload["action"] == "supersede"
    assert payload["status"] == "superseded"
    assert payload["superseded_by"] == "pb_target"
    assert payload["changed"] is True
    assert Path(payload["backup_path"]).exists()
    assert Path(payload["receipt_path"]).exists()
    receipt = json.loads(Path(payload["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["operation_id"] == payload["operation_id"]
    assert receipt["schema_version"] == "playbook_operator_receipt.v2"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_source'").fetchone()
        assert row["status"] == "superseded"
        assert row["superseded_by"] == "pb_target"
        ledger = conn.execute(
            "SELECT status, receipt_state, receipt_path FROM operator_operations WHERE operation_id=?",
            (payload["operation_id"],),
        ).fetchone()
        assert tuple(ledger) == ("committed", "mirrored", payload["receipt_path"])
    finally:
        conn.close()


def test_playbooks_cli_list_and_dedupe_do_not_write_schema(tmp_path, monkeypatch):
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        create_playbook(conn, playbook_id="pb_a", scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.7)
        create_playbook(conn, playbook_id="pb_b", scope_id="scope-a", payload=_payload(), status="candidate", confidence=0.8)
    finally:
        conn.close()

    def fail_schema(_conn):
        raise AssertionError("read-only playbook commands must not run ensure_schema")

    monkeypatch.setattr(cli, "ensure_schema", fail_schema)

    list_payload = cli.build_payload(cli.parse_args(["list", "--db", str(db_path), "--json"]))
    dedupe_payload = cli.build_payload(cli.parse_args(["dedupe", "--db", str(db_path), "--json"]))

    assert list_payload["ok"] is True
    assert list_payload["count"] == 2
    assert dedupe_payload["ok"] is True
    assert dedupe_payload["count"] == 1


def test_playbooks_apply_missing_id_on_empty_db_is_strictly_zero_write(
    tmp_path: Path,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    db_path.write_bytes(b"")
    before = (db_path.read_bytes(), db_path.stat().st_size, db_path.stat().st_mtime_ns)
    args = cli.parse_args(
        [
            "promote",
            "--db",
            str(db_path),
            "--scope-id",
            "scope-a",
            "--id",
            "missing-playbook",
            "--apply",
        ]
    )

    payload = cli.build_payload(args)

    after = (db_path.read_bytes(), db_path.stat().st_size, db_path.stat().st_mtime_ns)
    assert payload["ok"] is False
    assert payload["error"] == "not_found"
    assert after == before
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 0
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / "receipts").exists()


def test_playbooks_apply_backs_up_old_schema_before_writer_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    legacy_snapshot = _database_snapshot(db_path)

    events: list[str] = []
    original_backup = cli._backup_db
    original_ensure_schema = cli.ensure_schema

    def tracked_backup(path: Path) -> str:
        events.append("backup")
        return original_backup(path)

    def tracked_ensure_schema(
        conn: sqlite3.Connection,
        *,
        commit: bool = True,
    ) -> None:
        events.append("ensure_schema")
        original_ensure_schema(conn, commit=commit)

    monkeypatch.setattr(cli, "_backup_db", tracked_backup)
    monkeypatch.setattr(cli, "ensure_schema", tracked_ensure_schema)
    args = cli.parse_args(
        [
            "promote",
            "--db",
            str(db_path),
            "--scope-id",
            "scope-a",
            "--id",
            "pb_old_schema",
            "--reason",
            "reviewed migration-order fixture",
            "--apply",
        ]
    )

    payload = cli.build_payload(args)

    assert payload["ok"] is True
    assert events == ["backup", "ensure_schema"]
    backup_path = Path(payload["backup_path"])
    assert _database_snapshot(backup_path) == legacy_snapshot
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as live:
        assert live.execute("PRAGMA user_version").fetchone()[0] > 0
        assert live.execute(
            "SELECT status FROM procedural_playbooks WHERE id='pb_old_schema'"
        ).fetchone()[0] == "promoted"


def test_playbooks_backup_runs_under_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    original_backup = cli._backup_db
    contender_blocked = False

    def verify_locked_backup(path: Path) -> str:
        nonlocal contender_blocked
        contender = sqlite3.connect(path, timeout=0.0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute(
                    "UPDATE procedural_playbooks SET title='racing writer' "
                    "WHERE id='pb_old_schema'"
                )
            contender_blocked = True
        finally:
            contender.close()
        return original_backup(path)

    monkeypatch.setattr(cli, "_backup_db", verify_locked_backup)
    payload = cli.build_payload(_playbook_apply_args(cli, db_path))

    assert payload["ok"] is True
    assert contender_blocked is True


def test_playbooks_apply_rejects_semantically_invalid_persisted_candidate_without_writes(
    tmp_path: Path,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE procedural_playbooks SET steps='[]' WHERE id='pb_old_schema'"
        )
    before_db = _database_snapshot(db_path)
    before_artifacts = _operator_artifact_snapshot(tmp_path)

    payload = cli.build_payload(_playbook_apply_args(cli, db_path))

    assert payload["ok"] is False
    assert payload["error"] == "semantic_validation_failed"
    assert _database_snapshot(db_path) == before_db
    assert _operator_artifact_snapshot(tmp_path) == before_artifacts


def test_playbooks_apply_migration_failure_rolls_back_schema_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    before_db = _database_snapshot(db_path)
    before_artifacts = _operator_artifact_snapshot(tmp_path)
    original_ensure_schema = cli.ensure_schema

    def fail_after_migration(
        conn: sqlite3.Connection,
        *,
        commit: bool = True,
    ) -> None:
        original_ensure_schema(conn, commit=commit)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(cli, "ensure_schema", fail_after_migration)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        cli.build_payload(_playbook_apply_args(cli, db_path))

    assert _database_snapshot(db_path) == before_db
    assert _operator_artifact_snapshot(tmp_path) == before_artifacts


def test_playbooks_apply_receipt_failure_keeps_committed_ledger_and_is_repairable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    original_mirror = cli.mirror_operator_receipt

    def fail_receipt(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TypeError("injected receipt serialization failure")

    monkeypatch.setattr(cli, "mirror_operator_receipt", fail_receipt)
    payload = cli.build_payload(_playbook_apply_args(cli, db_path))

    assert payload["ok"] is True
    assert payload["status"] == "promoted"
    assert payload["receipt_state"] == "pending"
    assert payload["receipt_repair_required"] is True
    assert Path(payload["backup_path"]).exists()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        assert conn.execute(
            "SELECT status FROM procedural_playbooks WHERE id='pb_old_schema'"
        ).fetchone()[0] == "promoted"
        ledger = conn.execute(
            "SELECT status, receipt_state FROM operator_operations WHERE operation_id=?",
            (payload["operation_id"],),
        ).fetchone()
    assert ledger == ("committed", "pending")
    assert not list((tmp_path / "receipts").glob("*.json"))

    monkeypatch.setattr(cli, "mirror_operator_receipt", original_mirror)
    repair = cli.build_payload(
        cli.parse_args(
            ["receipts", "--db", str(db_path), "--apply", "--include-failed"]
        )
    )
    assert repair["ok"] is True
    assert repair["repair"]["mirrored"] == 1
    assert repair["report"]["unresolved"] == 0
    assert len(list((tmp_path / "receipts").glob("*.json"))) == 1


def test_playbooks_apply_receipt_publish_failure_leaves_repairable_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    mirror_globals = cli.mirror_operator_receipt.__globals__
    original_publish = mirror_globals["_publish_receipt_link"]

    def fail_receipt_link(source: Path, destination: Path) -> None:
        if Path(destination).parent.name == "receipts":
            raise OSError("injected receipt publish failure")
        original_publish(source, destination)

    monkeypatch.setitem(mirror_globals, "_publish_receipt_link", fail_receipt_link)
    payload = cli.build_payload(_playbook_apply_args(cli, db_path))

    assert payload["ok"] is True
    assert payload["status"] == "promoted"
    assert payload["receipt_state"] == "pending"
    assert payload["receipt_repair_required"] is True
    assert not list((tmp_path / "receipts").glob("*.tmp"))
    assert not list((tmp_path / "receipts").glob("*.json"))

    monkeypatch.setitem(mirror_globals, "_publish_receipt_link", original_publish)
    repair = cli.build_payload(
        cli.parse_args(["receipts", "--db", str(db_path), "--apply"])
    )
    assert repair["ok"] is True
    assert repair["repair"]["mirrored"] == 1
    assert repair["report"]["unresolved"] == 0
    assert len(list((tmp_path / "receipts").glob("*.json"))) == 1


def test_playbooks_apply_commit_failure_removes_receipt_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    before_db = _database_snapshot(db_path)
    before_artifacts = _operator_artifact_snapshot(tmp_path)
    original_connect = cli._connect

    class CommitFailProxy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

        def commit(self) -> None:
            raise sqlite3.OperationalError("injected apply commit failure")

    def failing_connect(path: Path, *, read_only: bool = False) -> object:
        conn = original_connect(path, read_only=read_only)
        return conn if read_only else CommitFailProxy(conn)

    monkeypatch.setattr(cli, "_connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match="injected apply commit failure"):
        cli.build_payload(_playbook_apply_args(cli, db_path))

    assert _database_snapshot(db_path) == before_db
    assert _operator_artifact_snapshot(tmp_path) == before_artifacts


def test_playbooks_apply_rejects_preview_to_writer_toctou_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_playbooks_cli()
    db_path = tmp_path / "memory.sqlite3"
    _create_legacy_playbook_db(db_path)
    before_artifacts = _operator_artifact_snapshot(tmp_path)
    original_connect = cli._connect
    tampered = False

    def tampering_connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
        nonlocal tampered
        if not read_only and not tampered:
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "UPDATE procedural_playbooks SET title=? WHERE id='pb_old_schema'",
                    ("Tampered after preview",),
                )
            tampered = True
        return original_connect(path, read_only=read_only)

    monkeypatch.setattr(cli, "_connect", tampering_connect)
    payload = cli.build_payload(_playbook_apply_args(cli, db_path))

    assert payload["ok"] is False
    assert payload["error"] == "validation_changed_before_lock"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT title, status FROM procedural_playbooks WHERE id='pb_old_schema'"
        ).fetchone()
        version_count = conn.execute(
            "SELECT COUNT(*) FROM playbook_versions WHERE playbook_id='pb_old_schema'"
        ).fetchone()[0]
    assert row == ("Tampered after preview", "candidate")
    assert version_count == 1
    assert _operator_artifact_snapshot(tmp_path) == before_artifacts


def test_merge_playbooks_rejects_private_shared_owner_mismatch():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_shared_target", scope_id="scope-owner", shared_scope_id="pool", payload=_payload(), confidence=0.9)
    create_playbook(conn, playbook_id="pb_consumer_private", scope_id="scope-consumer", shared_scope_id="", payload=_payload(), confidence=0.7)
    before_changes = conn.total_changes

    dry = merge_playbooks(
        conn,
        target_id="pb_shared_target",
        source_ids=["pb_consumer_private"],
        accessible_scope_ids=["scope-consumer", "pool"],
        reason="must not cross owner scope",
        dry_run=True,
    )
    applied = merge_playbooks(
        conn,
        target_id="pb_shared_target",
        source_ids=["pb_consumer_private"],
        accessible_scope_ids=["scope-consumer", "pool"],
        reason="must not cross owner scope",
        dry_run=False,
    )

    assert dry == {
        "merged": False,
        "dry_run": True,
        "target_id": "pb_shared_target",
        "error": "scope_owner_mismatch",
        "source_ids": ["pb_consumer_private"],
    }
    assert applied == {
        "merged": False,
        "dry_run": False,
        "target_id": "pb_shared_target",
        "error": "scope_owner_mismatch",
        "source_ids": ["pb_consumer_private"],
    }
    assert conn.total_changes == before_changes
    source = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_consumer_private'").fetchone()
    target = conn.execute("SELECT status, superseded_by FROM procedural_playbooks WHERE id = 'pb_shared_target'").fetchone()
    assert source["status"] == "candidate"
    assert source["superseded_by"] == ""
    assert target["status"] == "candidate"
    assert target["superseded_by"] == ""


def test_feedback_cannot_mutate_playbook_outside_accessible_scope():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_hidden")

    blocked = record_playbook_feedback(
        conn,
        playbook_id="pb_hidden",
        scope_id="scope-b",
        accessible_scope_ids=["scope-b"],
        outcome="failed",
        decision="guided_reuse",
        evidence=["should not be accepted"],
    )

    assert blocked == {"recorded": False, "id": "pb_hidden", "error": "not_found"}
    owner_view = inspect_playbook(conn, playbook_id="pb_hidden", accessible_scope_ids=["scope-a"])
    assert owner_view["playbook"]["status"] == "promoted"
    assert owner_view["playbook"]["failure_count"] == 0
    assert owner_view["runs"] == []


def test_shared_scope_feedback_records_private_run_without_mutating_global_playbook():
    conn = _conn()
    create_playbook(
        conn,
        playbook_id="pb_shared_feedback",
        scope_id="scope-owner",
        shared_scope_id="pool",
        payload=_payload(),
        status="candidate",
        confidence=0.9,
    )
    review_playbook(
        conn,
        playbook_id="pb_shared_feedback",
        accessible_scope_ids=["scope-owner", "pool"],
        action="promote",
        reason="fixture",
    )

    feedback = record_playbook_feedback(
        conn,
        playbook_id="pb_shared_feedback",
        scope_id="scope-consumer",
        accessible_scope_ids=["scope-consumer", "pool"],
        outcome="failed",
        decision="direct_reuse",
        evidence=["consumer environment failed"],
        outcome_reason="private failure",
    )

    owner_view = inspect_playbook(conn, playbook_id="pb_shared_feedback", accessible_scope_ids=["scope-owner"])
    consumer_view = inspect_playbook(conn, playbook_id="pb_shared_feedback", accessible_scope_ids=["scope-consumer", "pool"])

    assert feedback["recorded"] is True
    assert feedback["global_updated"] is False
    assert feedback["status"] == "promoted"
    assert feedback["failure_count"] == 0
    assert owner_view["playbook"]["status"] == "promoted"
    assert owner_view["playbook"]["confidence"] == 0.9
    assert owner_view["playbook"]["failure_count"] == 0
    assert owner_view["runs"] == []
    assert [run["outcome_reason"] for run in consumer_view["runs"]] == ["private failure"]


def test_feedback_rejects_terminal_status_playbooks_without_mutating_counts_or_runs():
    conn = _conn()
    for action, expected_status in [("quarantine", "quarantined"), ("supersede", "superseded")]:
        playbook_id = f"pb_{expected_status}"
        _create_promoted(conn, playbook_id=playbook_id)
        review_kwargs = {}
        if action == "supersede":
            _create_promoted(conn, playbook_id="pb_terminal_canonical")
            review_kwargs["superseded_by"] = "pb_terminal_canonical"
        review = review_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=["scope-a"], action=action, reason="terminal", **review_kwargs)
        assert review["reviewed"] is True

        feedback = record_playbook_feedback(
            conn,
            playbook_id=playbook_id,
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="failed",
            decision="guided_reuse",
            evidence=["must not mutate terminal status"],
        )
        inspected = inspect_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=["scope-a"])

        assert feedback == {"recorded": False, "id": playbook_id, "error": "terminal_status", "status": expected_status}
        assert inspected["playbook"]["status"] == expected_status
        assert inspected["playbook"]["failure_count"] == 0
        assert inspected["runs"] == []


def test_feedback_finalizes_pending_run_on_terminal_playbook_exactly_once():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_terminal_run")
    pending = record_experience_preflight_run(
        conn,
        playbook={"id": "pb_terminal_run", "confidence": 0.9, "preconditions": [], "steps": []},
        scope_id="scope-a",
        decision="guided_reuse",
        query="Need one-way Headscale ACL",
        reasons=["fixture"],
    )
    run_id = pending["run_id"]
    pending_row = conn.execute("SELECT outcome, finished_at FROM experience_runs WHERE id = ?", (run_id,)).fetchone()
    assert pending_row["outcome"] == "unknown"
    assert not pending_row["finished_at"]

    review_playbook(conn, playbook_id="pb_terminal_run", accessible_scope_ids=["scope-a"], action="quarantine", reason="terminal after pending run")
    before = conn.execute(
        "SELECT status, success_count, failure_count, stale_count, confidence FROM procedural_playbooks WHERE id = ?",
        ("pb_terminal_run",),
    ).fetchone()

    first = record_playbook_feedback(
        conn,
        playbook_id="pb_terminal_run",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="success",
        decision="guided_reuse",
        evidence=["late feedback after quarantine"],
        outcome_reason="finalize pending run",
        run_id=run_id,
    )
    after_first = conn.execute(
        "SELECT status, success_count, failure_count, stale_count, confidence FROM procedural_playbooks WHERE id = ?",
        ("pb_terminal_run",),
    ).fetchone()
    finalized = conn.execute(
        "SELECT outcome, outcome_reason, finished_at FROM experience_runs WHERE id = ?",
        (run_id,),
    ).fetchone()

    assert first["recorded"] is True
    assert first["global_updated"] is False
    assert first["run_finalized"] is True
    assert first["status"] == "quarantined"
    assert tuple(after_first) == tuple(before)
    assert finalized["outcome"] == "success"
    assert finalized["finished_at"]
    assert finalized["outcome_reason"] == "finalize pending run"

    second = record_playbook_feedback(
        conn,
        playbook_id="pb_terminal_run",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        outcome="failed",
        decision="guided_reuse",
        evidence=["must not finalize twice"],
        run_id=run_id,
    )
    after_second = conn.execute(
        "SELECT status, success_count, failure_count, outcome FROM procedural_playbooks "
        "JOIN experience_runs ON experience_runs.playbook_id = procedural_playbooks.id "
        "WHERE procedural_playbooks.id = ? AND experience_runs.id = ?",
        ("pb_terminal_run", run_id),
    ).fetchone()

    assert second["recorded"] is False
    assert second["error"] == "run_already_finalized"
    assert after_second["status"] == "quarantined"
    assert after_second["success_count"] == 0
    assert after_second["failure_count"] == 0
    assert after_second["outcome"] == "success"
    assert conn.execute("SELECT COUNT(*) FROM experience_runs WHERE playbook_id = ?", ("pb_terminal_run",)).fetchone()[0] == 1


def test_create_playbook_rejects_direct_promoted_status():
    conn = _conn()

    with pytest.raises(ExperienceValidationError):
        create_playbook(conn, playbook_id="pb_promoted", scope_id="scope-a", shared_scope_id="", payload=_payload(), status="promoted", confidence=0.9)

    assert conn.execute("SELECT COUNT(*) FROM procedural_playbooks WHERE id = ?", ("pb_promoted",)).fetchone()[0] == 0


def test_create_playbook_rejects_secret_like_content_and_packet_redacts_legacy_secret_rows():
    conn = _conn()
    secret_payload = _payload()
    secret_payload["steps"][0]["action"] = "Use api_key=not_a_real_key_12345 while editing policy."

    with pytest.raises(ExperienceValidationError):
        create_playbook(conn, playbook_id="pb_secret", scope_id="scope-a", shared_scope_id="", payload=secret_payload)


    create_playbook(conn, playbook_id="pb_legacy", scope_id="scope-a", shared_scope_id="", payload=_payload(), status="candidate", confidence=0.95)
    review_playbook(conn, playbook_id="pb_legacy", accessible_scope_ids=["scope-a"], action="promote", reason="fixture")
    legacy_steps = '[{"number": 1, "capability_class": "read_only", "action": "Read policy with token=legacy_token_example_12345", "evidence_required": "policy"}]'
    conn.execute("UPDATE procedural_playbooks SET steps = ? WHERE id = ?", (legacy_steps, "pb_legacy"))
    conn.execute("DELETE FROM procedural_playbooks_fts WHERE playbook_id = ?", ("pb_legacy",))
    conn.execute(
        "INSERT INTO procedural_playbooks_fts(playbook_id, title, trigger, goal, preconditions, steps, pitfalls, verification) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("pb_legacy", "Headscale one-way ACL", "User asks one-way management", "Apply safely", "[]", legacy_steps, "[]", "policy validates"),
    )
    conn.commit()

    result = experience_preflight(conn, query="one-way management policy", accessible_scope_ids=["scope-a"], config={})
    serialized_result = json.dumps(result, ensure_ascii=False)

    assert result["packet"]
    assert "***" not in result["packet"]
    assert "token=" not in result["packet"].lower()
    assert "[REDACTED_SECRET]" in result["packet"]
    assert "legacy_token_example_12345" not in serialized_result
    assert "token=" not in serialized_result.lower()


def test_feedback_rejects_secret_like_evidence_before_persisting():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_feedback_secret")

    with pytest.raises(ExperienceValidationError):
        record_playbook_feedback(
            conn,
            playbook_id="pb_feedback_secret",
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="success",
            evidence=["api_key=not_a_real_key_12345"],
            outcome_reason="safe",
        )

    assert conn.execute("SELECT COUNT(*) FROM experience_runs WHERE playbook_id = ?", ("pb_feedback_secret",)).fetchone()[0] == 0


def test_create_rejects_secret_like_playbook_id_and_created_from_episode_id():
    conn = _conn()

    with pytest.raises(ExperienceValidationError):
        create_playbook(
            conn,
            playbook_id="token=not_a_real_key_12345",
            scope_id="scope-a",
            shared_scope_id="",
            payload=_payload(),
        )
    with pytest.raises(ExperienceValidationError):
        create_playbook(
            conn,
            playbook_id="pb_safe",
            scope_id="scope-a",
            shared_scope_id="",
            payload=_payload(),
            created_from_episode_id="api_key=not_a_real_key_12345",
        )

    assert conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0] == 0


def test_playbook_lookup_paths_do_not_echo_secret_like_playbook_id():
    conn = _conn()
    secret_id = "token=legacy_token_example_12345"

    inspected = inspect_playbook(conn, playbook_id=secret_id, accessible_scope_ids=["scope-a"])
    serialized_inspect = json.dumps(inspected, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized_inspect
    assert "token=" not in serialized_inspect.lower()
    assert "[REDACTED_SECRET]" in serialized_inspect
    with pytest.raises(ExperienceValidationError):
        review_playbook(conn, playbook_id=secret_id, accessible_scope_ids=["scope-a"], action="promote", reason="safe")
    with pytest.raises(ExperienceValidationError):
        record_playbook_feedback(
            conn,
            playbook_id=secret_id,
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="success",
            evidence=["safe"],
        )


def test_playbook_secret_like_mapping_keys_are_rejected_and_legacy_keys_are_redacted():
    conn = _conn()
    secret_key_payload = _payload()
    secret_key_payload["preconditions"][0]["token=not_a_real_key_12345"] = "do not persist key"

    with pytest.raises(ExperienceValidationError):
        create_playbook(conn, playbook_id="pb_secret_key", scope_id="scope-a", shared_scope_id="", payload=secret_key_payload)

    create_playbook(conn, playbook_id="pb_legacy_key", scope_id="scope-a", shared_scope_id="", payload=_payload(), status="candidate", confidence=0.95)
    review_playbook(conn, playbook_id="pb_legacy_key", accessible_scope_ids=["scope-a"], action="promote", reason="fixture")
    legacy_preconditions = '[{"token=legacy_token_example_12345": "legacy", "check": "Read live node list", "evidence_required": "node list"}]'
    conn.execute("UPDATE procedural_playbooks SET preconditions = ?, metadata = ? WHERE id = ?", (legacy_preconditions, '{"api_key=legacy_key_name_12345":"legacy"}', "pb_legacy_key"))
    conn.execute("DELETE FROM procedural_playbooks_fts WHERE playbook_id = ?", ("pb_legacy_key",))
    conn.execute(
        "INSERT INTO procedural_playbooks_fts(playbook_id, title, trigger, goal, preconditions, steps, pitfalls, verification) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("pb_legacy_key", "Headscale one-way ACL", "User asks one-way management", "Apply safely", legacy_preconditions, "[]", "[]", "policy validates"),
    )
    conn.commit()

    search_payload = search_playbooks(conn, query="one-way management policy", accessible_scope_ids=["scope-a"], limit=5)
    inspect_payload = inspect_playbook(conn, playbook_id="pb_legacy_key", accessible_scope_ids=["scope-a"])
    preflight_payload = experience_preflight(conn, query="one-way management policy", accessible_scope_ids=["scope-a"], config={})
    serialized = json.dumps({"search": search_payload, "inspect": inspect_payload, "preflight": preflight_payload}, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "legacy_key_name_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "api_key=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_inspect_redacts_legacy_review_change_reason():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_review_secret")
    conn.execute(
        "UPDATE playbook_versions SET change_reason = ? WHERE playbook_id = ? AND change_type = ?",
        ("token=legacy_token_example_12345", "pb_review_secret", "promoted"),
    )
    conn.commit()

    inspected = inspect_playbook(conn, playbook_id="pb_review_secret", accessible_scope_ids=["scope-a"])
    serialized = json.dumps(inspected, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_feedback_rejects_secret_like_decision_and_inspect_redacts_legacy_decision():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_decision_secret")

    with pytest.raises(ExperienceValidationError):
        record_playbook_feedback(
            conn,
            playbook_id="pb_decision_secret",
            scope_id="scope-a",
            accessible_scope_ids=["scope-a"],
            outcome="success",
            decision="token=not_a_real_key_12345",
            evidence=["safe"],
        )
    assert conn.execute("SELECT COUNT(*) FROM experience_runs WHERE playbook_id = ?", ("pb_decision_secret",)).fetchone()[0] == 0

    conn.execute(
        """
        INSERT INTO experience_runs(
            id, playbook_id, scope_id, decision, confidence_at_use, evidence, outcome,
            outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "xrun_legacy_decision",
            "pb_decision_secret",
            "scope-a",
            "token=legacy_token_example_12345",
            0.9,
            "[]",
            "success",
            "safe",
            "model",
            1,
            10,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()

    inspected = inspect_playbook(conn, playbook_id="pb_decision_secret", accessible_scope_ids=["scope-a"])
    serialized = json.dumps(inspected, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_inspect_and_stats_redact_legacy_secret_like_run_outcome():
    conn = _conn()
    _create_promoted(conn, playbook_id="pb_outcome_secret")
    conn.execute(
        """
        INSERT INTO experience_runs(
            id, playbook_id, scope_id, decision, confidence_at_use, evidence, outcome,
            outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "xrun_legacy_outcome",
            "pb_outcome_secret",
            "scope-a",
            "guided_reuse",
            0.9,
            "[]",
            "token=legacy_token_example_12345",
            "safe",
            "model",
            1,
            10,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.execute("UPDATE procedural_playbooks SET status = ? WHERE id = ?", ("token=legacy_status_example_12345", "pb_outcome_secret"))
    conn.commit()

    inspected = inspect_playbook(conn, playbook_id="pb_outcome_secret", accessible_scope_ids=["scope-a"])
    stats = experience_stats(conn, accessible_scope_ids=["scope-a"])
    serialized = json.dumps({"inspect": inspected, "stats": stats}, ensure_ascii=False)

    assert "legacy_token_example_12345" not in serialized
    assert "legacy_status_example_12345" not in serialized
    assert "token=" not in serialized.lower()
    assert "[REDACTED_SECRET]" in serialized


def test_shared_playbook_inspect_and_stats_filter_runs_by_run_scope():
    conn = _conn()
    create_playbook(conn, playbook_id="pb_shared", scope_id="scope-owner", shared_scope_id="pool", payload=_payload(), status="candidate", confidence=0.9)
    review_playbook(conn, playbook_id="pb_shared", accessible_scope_ids=["scope-owner", "pool"], action="promote", reason="fixture")
    record_playbook_feedback(
        conn,
        playbook_id="pb_shared",
        scope_id="scope-a",
        accessible_scope_ids=["scope-a", "pool"],
        outcome="success",
        outcome_reason="private-A",
    )
    record_playbook_feedback(
        conn,
        playbook_id="pb_shared",
        scope_id="scope-b",
        accessible_scope_ids=["scope-b", "pool"],
        outcome="failed",
        outcome_reason="private-B",
    )

    a_view = inspect_playbook(conn, playbook_id="pb_shared", accessible_scope_ids=["scope-a", "pool"])
    a_stats = experience_stats(conn, accessible_scope_ids=["scope-a", "pool"])

    assert a_view["found"] is True
    assert [run["outcome_reason"] for run in a_view["runs"]] == ["private-A"]
    assert a_stats["runs"]["total"] == 1
    assert a_stats["runs"]["by_outcome"] == {"success": 1}


def test_shared_pool_scope_never_proves_same_playbook_owner():
    conn = _conn()
    owner_a_scope = RuntimeScope(
        platform="telegram",
        user_id="fixture-user",
        agent_identity="agent-a",
        agent_workspace="hermes",
    )
    owner_b_scope = RuntimeScope(
        platform="telegram",
        user_id="fixture-user",
        agent_identity="agent-b",
        agent_workspace="hermes",
    )
    owner_a = build_shared_scope_id(owner_a_scope)
    owner_b = build_shared_scope_id(owner_b_scope)
    shared_pool = build_shared_pool_scope_id(owner_a_scope, "fixture-pool")
    accessible = [owner_a, owner_b, shared_pool]
    create_playbook(
        conn,
        playbook_id="pool_target",
        scope_id=owner_a,
        shared_scope_id=shared_pool,
        payload=_payload(),
        confidence=0.9,
    )
    create_playbook(
        conn,
        playbook_id="pool_source",
        scope_id=owner_b,
        shared_scope_id=shared_pool,
        payload=_payload(),
        confidence=0.7,
    )

    groups = find_duplicate_playbooks(
        conn,
        accessible_scope_ids=accessible,
        limit=10,
    )
    reviewed = review_playbook(
        conn,
        playbook_id="pool_source",
        accessible_scope_ids=accessible,
        action="supersede",
        superseded_by="pool_target",
        reason="must remain agent-owned",
        dry_run=True,
    )
    merged = merge_playbooks(
        conn,
        target_id="pool_target",
        source_ids=["pool_source"],
        accessible_scope_ids=accessible,
        reason="must remain agent-owned",
        dry_run=True,
    )

    assert groups == []
    assert reviewed["error"] == "superseded_by_scope_mismatch"
    assert merged["error"] == "scope_owner_mismatch"


def test_owner_alias_group_rejects_structured_shared_pool_scope():
    conn = _conn()
    owner_a_scope = RuntimeScope(
        platform="telegram",
        user_id="fixture-user",
        agent_identity="agent-a",
        agent_workspace="hermes",
    )
    owner_b_scope = RuntimeScope(
        platform="telegram",
        user_id="fixture-user",
        agent_identity="agent-b",
        agent_workspace="hermes",
    )
    owner_a = build_shared_scope_id(owner_a_scope)
    owner_b = build_shared_scope_id(owner_b_scope)
    shared_pool = build_shared_pool_scope_id(owner_a_scope, "fixture-pool")
    accessible = [owner_a, owner_b, shared_pool]
    create_playbook(
        conn,
        playbook_id="alias_target",
        scope_id=owner_a,
        payload=_payload(),
        confidence=0.9,
    )
    create_playbook(
        conn,
        playbook_id="alias_source",
        scope_id=owner_b,
        payload=_payload(),
        confidence=0.7,
    )

    reviewed = review_playbook(
        conn,
        playbook_id="alias_source",
        accessible_scope_ids=accessible,
        owner_scope_aliases=[owner_a, owner_b, shared_pool],
        action="supersede",
        superseded_by="alias_target",
        reason="pool must not become owner capability",
        dry_run=True,
    )
    merged = merge_playbooks(
        conn,
        target_id="alias_target",
        source_ids=["alias_source"],
        accessible_scope_ids=accessible,
        owner_scope_aliases=[owner_a, owner_b, shared_pool],
        reason="pool must not become owner capability",
        dry_run=True,
    )
    groups = find_duplicate_playbooks(
        conn,
        accessible_scope_ids=accessible,
        owner_scope_aliases=[owner_a, owner_b, shared_pool],
        limit=10,
    )

    assert reviewed["error"] == "owner_scope_alias_is_shared_pool"
    assert merged["error"] == "owner_scope_alias_is_shared_pool"
    assert groups == []


def test_review_rechecks_authoritative_rows_after_validation_token_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    db_path = tmp_path / "review-race.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    writer = sqlite3.connect(db_path)
    create_playbook(
        conn,
        playbook_id="review_target",
        scope_id="owner-a",
        payload=_payload(),
        confidence=0.9,
    )
    create_playbook(
        conn,
        playbook_id="review_source",
        scope_id="owner-a",
        payload=_payload(),
        confidence=0.7,
    )
    accessible = ["owner-a", "owner-victim"]
    dry_run = review_playbook(
        conn,
        playbook_id="review_source",
        accessible_scope_ids=accessible,
        action="supersede",
        superseded_by="review_target",
        reason="race fixture",
        dry_run=True,
    )
    original = experience_store_module._review_validation_token
    raced = False

    def race_after_token(*args, **kwargs):
        nonlocal raced
        token = original(*args, **kwargs)
        if not raced:
            raced = True
            writer.execute(
                "UPDATE procedural_playbooks SET scope_id = ?, updated_at = ? WHERE id = ?",
                ("owner-victim", "2099-01-01T00:00:00+00:00", "review_source"),
            )
            writer.commit()
        return token

    monkeypatch.setattr(
        experience_store_module,
        "_review_validation_token",
        race_after_token,
    )
    applied = review_playbook(
        conn,
        playbook_id="review_source",
        accessible_scope_ids=accessible,
        action="supersede",
        superseded_by="review_target",
        reason="race fixture",
        validated_payload=dry_run,
    )
    row = conn.execute(
        "SELECT scope_id, status, superseded_by FROM procedural_playbooks WHERE id = ?",
        ("review_source",),
    ).fetchone()

    assert applied["error"] == "stale_validation"
    assert dict(row) == {
        "scope_id": "owner-victim",
        "status": "candidate",
        "superseded_by": "",
    }
    writer.close()
    conn.close()


def test_merge_rechecks_authoritative_rows_after_owner_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    db_path = tmp_path / "merge-race.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    writer = sqlite3.connect(db_path)
    create_playbook(
        conn,
        playbook_id="merge_target",
        scope_id="owner-a",
        payload=_payload(),
        confidence=0.9,
    )
    create_playbook(
        conn,
        playbook_id="merge_source",
        scope_id="owner-a",
        payload=_payload(),
        confidence=0.7,
    )
    accessible = ["owner-a", "owner-victim"]
    original = experience_store_module._same_owner_scope
    raced = False

    def race_after_owner_check(left, right, owner_scope_aliases=()):
        nonlocal raced
        same_owner = original(left, right, owner_scope_aliases)
        if not raced:
            raced = True
            writer.execute(
                "UPDATE procedural_playbooks SET scope_id = ?, updated_at = ? WHERE id = ?",
                ("owner-victim", "2099-01-01T00:00:00+00:00", "merge_source"),
            )
            writer.commit()
        return same_owner

    monkeypatch.setattr(
        experience_store_module,
        "_same_owner_scope",
        race_after_owner_check,
    )
    applied = merge_playbooks(
        conn,
        target_id="merge_target",
        source_ids=["merge_source"],
        accessible_scope_ids=accessible,
        reason="race fixture",
        dry_run=False,
    )
    row = conn.execute(
        "SELECT scope_id, status, superseded_by FROM procedural_playbooks WHERE id = ?",
        ("merge_source",),
    ).fetchone()

    assert applied["error"] == "stale_validation"
    assert dict(row) == {
        "scope_id": "owner-victim",
        "status": "candidate",
        "superseded_by": "",
    }
    writer.close()
    conn.close()
