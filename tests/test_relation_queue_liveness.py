"""Liveness contracts for monotonic relation rebuild progress."""

from __future__ import annotations

# Historical counterexamples for the removed legacy worker. Program 0 replaces
# this engine with finite containment/focus work and tests that live contract in
# test_relation_containment.py and test_relation_rebuild_retirement.py.
__test__ = False

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import scope_recall.relation_extraction as relation_extraction
import scope_recall.relation_frequency_index as relation_frequency_index
import scope_recall.relation_rebuild_queue as relation_rebuild_queue
from scope_recall.relation_rebuild_queue import (
    claim_relation_rebuild_events,
    drain_relation_rebuild_queue,
    enqueue_relation_rebuild,
)
from scope_recall.sql_store import ensure_schema, store_row


def _store(
    conn: sqlite3.Connection,
    memory_id: str,
    timestamp: str,
    *,
    scope_id: str = "scope-live",
    entity: str = "Queue Liveness",
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        platform="test",
        user_id="joy",
        chat_id="chat",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="test",
        session_id="session",
        source="fixture",
        target="project",
        content=f"{entity} records an unrelated durable operational fact",
        metadata=json.dumps({"entities": [entity]}),
        allow_duplicate=True,
        timestamp=timestamp,
        enqueue_vector_intent=False,
    )


def test_relation_queue_progress_is_monotonic_during_twelve_interleaved_writes(
    tmp_path: Path,
) -> None:
    """The former revision-reset counterexample must finish after writes stop."""

    db_path = tmp_path / "relation-monotonic.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store(conn, "focus", "2026-07-21T00:00:00+00:00")
    for index in range(50):
        _store(
            conn,
            f"peer-{index:03d}",
            f"2026-07-21T00:00:{index + 1:02d}+00:00",
        )
    conn.execute("DELETE FROM relation_rebuild_queue")
    enqueue_relation_rebuild(
        conn,
        scope_id="scope-live",
        focus_memory_id="focus",
        requested_updated_at="2026-07-21T00:00:00+00:00",
        reason="monotonic liveness regression",
        commit=True,
    )

    previous_cursor = ""
    previous_processed = 0
    previous_attempts = 0
    for cycle in range(12):
        result = drain_relation_rebuild_queue(
            conn,
            max_events=1,
            pair_limit=3,
            worker_id="monotonic-worker",
        )
        assert result["chunks_completed"] == 1
        row = conn.execute(
            """
            SELECT status, cursor_memory_id, processed_pairs, attempts, failures
            FROM relation_rebuild_queue
            WHERE scope_id='scope-live' AND focus_memory_id='focus'
            """
        ).fetchone()
        assert str(row[0]) == "pending"
        assert str(row[1]) > previous_cursor
        assert int(row[2]) > previous_processed
        assert int(row[3]) > previous_attempts
        assert int(row[4]) == 0
        previous_cursor = str(row[1])
        previous_processed = int(row[2])
        previous_attempts = int(row[3])

        # The new peer sorts after the current cursor.  Its truth transaction
        # updates the frequency companion, but never resets this focus pass.
        _store(
            conn,
            f"zz-new-peer-{cycle:02d}",
            f"2026-07-21T01:00:{cycle:02d}+00:00",
        )

    final_result = drain_relation_rebuild_queue(
        conn,
        max_events=100,
        pair_limit=3,
        worker_id="monotonic-worker",
    )
    assert final_result["events_completed"] == 1
    row = conn.execute(
        """
        SELECT status, cursor_memory_id, processed_pairs, attempts, failures,
               pass_number, supersession_count
        FROM relation_rebuild_queue
        WHERE scope_id='scope-live' AND focus_memory_id='focus'
        """
    ).fetchone()
    assert str(row[0]) == "completed"
    assert str(row[1]) == "zz-new-peer-11"
    assert int(row[2]) == 62
    assert int(row[3]) >= previous_attempts
    assert int(row[4]) == 0
    assert int(row[5]) == 1
    assert int(row[6]) == 0
    conn.close()


def test_relation_queue_defers_when_frequency_receipt_cas_loses_cross_connection_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale blocked-entity receipt must never bind or advance a queue pass."""

    db_path = tmp_path / "relation-frequency-cas.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        for index in range(19):
            _store(
                conn,
                "focus" if index == 0 else f"peer-{index:02d}",
                f"2026-07-21T02:00:{index:02d}+00:00",
                scope_id="scope-cas",
                entity="Common Hub",
            )
        conn.execute("DELETE FROM relation_rebuild_queue")
        enqueue_relation_rebuild(
            conn,
            scope_id="scope-cas",
            focus_memory_id="focus",
            requested_updated_at="2026-07-21T02:00:00+00:00",
            reason="cross-connection receipt CAS regression",
            commit=False,
        )
        conn.execute(
            """
            UPDATE relation_scope_statistics
            SET statistics_revision=-1, blocked_entities_json='[]',
                blocked_entities_sha256=''
            WHERE scope_id='scope-cas'
            """
        )
        conn.commit()

        original_counts = relation_frequency_index._blocked_entities_from_counts
        interleaved = False

        def cross_threshold_during_refresh(
            connection: sqlite3.Connection,
            *,
            scope_id: str,
            visible_memory_count: int,
        ) -> set[str]:
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                _store(
                    writer,
                    "peer-19",
                    "2026-07-21T02:00:19+00:00",
                    scope_id="scope-cas",
                    entity="Common Hub",
                )
            return original_counts(
                connection,
                scope_id=scope_id,
                visible_memory_count=visible_memory_count,
            )

        monkeypatch.setattr(
            relation_frequency_index,
            "_blocked_entities_from_counts",
            cross_threshold_during_refresh,
        )
        result = drain_relation_rebuild_queue(
            conn,
            max_events=1,
            pair_limit=1,
            worker_id="receipt-cas-worker",
        )

        queue_row = conn.execute(
            """
            SELECT status, corpus_revision, blocked_entities_json, processed_pairs
            FROM relation_rebuild_queue
            WHERE scope_id='scope-cas' AND focus_memory_id='focus'
            """
        ).fetchone()
        scope_row = conn.execute(
            """
            SELECT corpus_revision, statistics_revision, visible_memory_count,
                   blocked_entities_json
            FROM relation_scope_statistics WHERE scope_id='scope-cas'
            """
        ).fetchone()

        assert interleaved is True
        assert dict(result) == {
            "claimed": 1,
            "chunks_completed": 0,
            "events_completed": 0,
            "superseded": 1,
            "failed": 0,
            "dead_lettered": 0,
        }
        assert str(queue_row[0]) == "pending"
        assert int(queue_row[1]) != 19
        assert int(queue_row[3]) == 0
        assert int(scope_row[0]) == 20
        assert int(scope_row[1]) == 20
        assert int(scope_row[2]) == 20
        assert "common hub" in json.loads(str(scope_row[3]))
    finally:
        conn.close()
        writer.close()


def test_relation_queue_rolls_back_when_corpus_changes_after_receipt_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final relation/cursor commit must CAS the bound corpus revision."""

    db_path = tmp_path / "relation-final-cas.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        for index in range(19):
            _store(
                conn,
                "focus" if index == 0 else f"peer-{index:02d}",
                f"2026-07-21T03:00:{index:02d}+00:00",
                scope_id="scope-final-cas",
                entity="Common Hub",
            )
        conn.execute("DELETE FROM memory_relations")
        conn.execute("DELETE FROM relation_rebuild_queue")
        enqueue_relation_rebuild(
            conn,
            scope_id="scope-final-cas",
            focus_memory_id="focus",
            requested_updated_at="2026-07-21T03:00:00+00:00",
            reason="final relation transaction CAS regression",
            commit=True,
        )

        original_rebuild = relation_extraction.rebuild_extracted_relations
        interleaved = False

        def cross_threshold_after_receipt_commit(*args, **kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                _store(
                    writer,
                    "peer-19",
                    "2026-07-21T03:00:19+00:00",
                    scope_id="scope-final-cas",
                    entity="Common Hub",
                )
            return original_rebuild(*args, **kwargs)

        monkeypatch.setattr(
            relation_extraction,
            "rebuild_extracted_relations",
            cross_threshold_after_receipt_commit,
        )
        result = drain_relation_rebuild_queue(
            conn,
            max_events=1,
            pair_limit=1,
            worker_id="final-cas-worker",
        )

        queue_row = conn.execute(
            """
            SELECT status, processed_pairs, pass_processed_pairs,
                   supersession_count, lease_owner
            FROM relation_rebuild_queue
            WHERE scope_id='scope-final-cas' AND focus_memory_id='focus'
            """
        ).fetchone()
        scope_row = conn.execute(
            """
            SELECT corpus_revision, statistics_revision, blocked_entities_json
            FROM relation_scope_statistics WHERE scope_id='scope-final-cas'
            """
        ).fetchone()
        generated_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM memory_relations
                WHERE source_memory_id='focus' OR target_memory_id='focus'
                """
            ).fetchone()[0]
        )

        assert interleaved is True
        assert result == {
            "claimed": 1,
            "chunks_completed": 0,
            "events_completed": 0,
            "superseded": 1,
            "failed": 0,
            "dead_lettered": 0,
        }
        assert tuple(queue_row) == ("pending", 0, 0, 1, "")
        assert int(scope_row[0]) == 20
        assert int(scope_row[1]) == 20
        assert "common hub" in json.loads(str(scope_row[2]))
        assert generated_count == 0
    finally:
        conn.close()
        writer.close()


def test_expired_relation_leases_dead_letter_and_yield_to_later_work(
    tmp_path: Path,
) -> None:
    """Repeated claim-and-crash cycles must consume a bounded recovery budget."""

    conn = sqlite3.connect(tmp_path / "relation-lease-budget.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        enqueue_relation_rebuild(
            conn,
            scope_id="scope-poison",
            focus_memory_id="poison",
            requested_updated_at="revision-poison",
            reason="lease expiry budget regression",
            commit=True,
        )
        for cycle in range(5):
            claimed = claim_relation_rebuild_events(
                conn,
                worker_id=f"crashing-worker-{cycle}",
                limit=1,
                lease_seconds=0,
                commit=True,
            )
            assert len(claimed) == 1
            assert claimed[0]["focus_memory_id"] == "poison"

        enqueue_relation_rebuild(
            conn,
            scope_id="scope-healthy",
            focus_memory_id="healthy",
            requested_updated_at="revision-healthy",
            reason="fairness control",
            commit=True,
        )
        claimed = claim_relation_rebuild_events(
            conn,
            worker_id="healthy-worker",
            limit=1,
            lease_seconds=120,
            commit=True,
        )

        poison_status = conn.execute(
            """
            SELECT status, attempts, failures, lease_owner
            FROM relation_rebuild_queue
            WHERE scope_id='scope-poison' AND focus_memory_id='poison'
            """
        ).fetchone()
        assert len(claimed) == 1
        assert claimed[0]["focus_memory_id"] == "healthy"
        assert tuple(poison_status) == ("dead_letter", 5, 0, "")

        expiry_counts = conn.execute(
            """
            SELECT lease_expirations, pass_lease_expirations
            FROM relation_rebuild_queue
            WHERE scope_id='scope-poison' AND focus_memory_id='poison'
            """
        ).fetchone()
        assert tuple(expiry_counts) == (5, 5)
    finally:
        conn.close()


def test_successful_relation_chunk_resets_current_pass_expiry_budget(
    tmp_path: Path,
) -> None:
    """A recovered worker keeps lifetime telemetry but clears the active streak."""

    conn = sqlite3.connect(tmp_path / "relation-lease-recovery.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        _store(
            conn,
            "focus",
            "2026-07-21T04:00:00+00:00",
            scope_id="scope-recovery",
            entity="Recovery Project",
        )
        _store(
            conn,
            "peer",
            "2026-07-21T04:00:01+00:00",
            scope_id="scope-recovery",
            entity="Recovery Project",
        )
        conn.execute("DELETE FROM relation_rebuild_queue")
        enqueue_relation_rebuild(
            conn,
            scope_id="scope-recovery",
            focus_memory_id="focus",
            requested_updated_at="2026-07-21T04:00:00+00:00",
            reason="successful lease recovery control",
            commit=True,
        )
        first_claim = claim_relation_rebuild_events(
            conn,
            worker_id="crashed-once",
            limit=1,
            lease_seconds=0,
            commit=True,
        )
        assert len(first_claim) == 1

        result = drain_relation_rebuild_queue(
            conn,
            max_events=1,
            pair_limit=1,
            lease_seconds=120,
            worker_id="recovered-worker",
        )
        row = conn.execute(
            """
            SELECT status, lease_expirations, pass_lease_expirations,
                   attempts, failures
            FROM relation_rebuild_queue
            WHERE scope_id='scope-recovery' AND focus_memory_id='focus'
            """
        ).fetchone()

        assert result["chunks_completed"] == 1
        assert result["events_completed"] == 1
        assert tuple(row) == ("completed", 1, 0, 2, 0)
    finally:
        conn.close()


def test_expired_relation_lease_budget_promotes_newer_revision(
    tmp_path: Path,
) -> None:
    """A poisoned old pass must not dead-letter newer requested truth."""

    conn = sqlite3.connect(tmp_path / "relation-lease-newer-revision.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        enqueue_relation_rebuild(
            conn,
            scope_id="scope-newer-revision",
            focus_memory_id="poison",
            requested_updated_at="revision-old",
            reason="old poisoned pass",
            commit=True,
        )
        for cycle in range(5):
            claimed = claim_relation_rebuild_events(
                conn,
                worker_id=f"old-crashing-worker-{cycle}",
                limit=1,
                lease_seconds=0,
                commit=True,
            )
            assert len(claimed) == 1
            assert claimed[0]["requested_updated_at"] == "revision-old"

        enqueue_relation_rebuild(
            conn,
            scope_id="scope-newer-revision",
            focus_memory_id="poison",
            requested_updated_at="revision-new",
            reason="new truth revision",
            commit=True,
        )
        claimed = claim_relation_rebuild_events(
            conn,
            worker_id="new-revision-worker",
            limit=1,
            lease_seconds=120,
            commit=True,
        )
        row = conn.execute(
            """
            SELECT status, requested_updated_at, next_requested_updated_at,
                   pass_number, attempts, lease_expirations,
                   pass_lease_expirations, lease_owner
            FROM relation_rebuild_queue
            WHERE scope_id='scope-newer-revision' AND focus_memory_id='poison'
            """
        ).fetchone()

        assert len(claimed) == 1
        assert claimed[0]["requested_updated_at"] == "revision-new"
        assert tuple(row) == (
            "processing",
            "revision-new",
            "",
            2,
            6,
            5,
            0,
            "new-revision-worker",
        )
    finally:
        conn.close()


def test_relation_queue_bounds_continuous_corpus_supersession_then_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous corpus churn must yield instead of retrying one chunk forever."""

    db_path = tmp_path / "relation-continuous-corpus-churn.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    writer = sqlite3.connect(db_path, timeout=0)
    writer.row_factory = sqlite3.Row
    ensure_schema(conn)
    clock = [datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(relation_rebuild_queue, "_now", lambda: clock[0])
    try:
        _store(conn, "focus", "2026-07-21T00:00:00+00:00")
        for index in range(4):
            _store(
                conn,
                f"peer-{index:02d}",
                f"2026-07-21T00:00:{index + 1:02d}+00:00",
            )
        conn.execute("DELETE FROM relation_rebuild_queue")
        enqueue_relation_rebuild(
            conn,
            scope_id="scope-live",
            focus_memory_id="focus",
            requested_updated_at="2026-07-21T00:00:00+00:00",
            reason="continuous corpus supersession regression",
            commit=True,
        )

        original_rebuild = relation_extraction.rebuild_extracted_relations
        writes = 0
        blocked_writes = 0
        deferred_write = False
        precommit_changes_remaining = 2

        def change_corpus() -> None:
            nonlocal writes
            memory_id = "peer-03"
            changed_at = datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc) + timedelta(
                seconds=writes
            )
            writer.execute(
                "UPDATE memories SET updated_at=? WHERE id=?",
                (changed_at.isoformat(), memory_id),
            )
            relation_frequency_index.sync_relation_frequency_memory(
                writer,
                memory_id,
            )
            writer.commit()
            writes += 1

        def continuously_change_corpus(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal blocked_writes, deferred_write, precommit_changes_remaining
            if bool(kwargs.get("dry_run")):
                return original_rebuild(*args, **kwargs)
            if precommit_changes_remaining:
                change_corpus()
                precommit_changes_remaining -= 1
            result = original_rebuild(*args, **kwargs)
            try:
                change_corpus()
            except sqlite3.OperationalError as exc:
                writer.rollback()
                if "locked" not in str(exc).lower():
                    raise
                blocked_writes += 1
                deferred_write = True
            return result

        monkeypatch.setattr(
            relation_extraction,
            "rebuild_extracted_relations",
            continuously_change_corpus,
        )

        churn_result = relation_rebuild_queue.drain_relation_rebuild_queue(
            conn,
            max_events=32,
            pair_limit=1,
            worker_id="continuous-churn-worker",
        )
        churn_row = conn.execute(
            """
            SELECT status, available_at, cursor_memory_id, processed_pairs,
                   attempts, supersession_count, pass_number
            FROM relation_rebuild_queue
            WHERE scope_id='scope-live' AND focus_memory_id='focus'
            """
        ).fetchone()

        assert writes >= 2
        assert churn_result["claimed"] <= 2
        assert churn_result["superseded"] <= 2
        assert str(churn_row["status"]) == "pending"
        assert str(churn_row["cursor_memory_id"]) == ""
        assert int(churn_row["processed_pairs"]) == 0
        assert str(churn_row["available_at"]) > clock[0].isoformat()
        assert int(churn_row["attempts"]) == int(churn_result["claimed"])
        assert int(churn_row["supersession_count"]) >= int(churn_result["superseded"])
        assert int(churn_row["pass_number"]) >= 1
        report = relation_rebuild_queue.relation_rebuild_queue_report(conn)
        sample = report["samples"][0]
        assert sample["available_at"] == str(churn_row["available_at"])
        assert sample["corpus_revision"] == 0
        assert report["supersession_count"] >= int(churn_row["supersession_count"])

        previous_processed = 0
        previous_attempts = int(churn_row["attempts"])
        previous_supersessions = int(churn_row["supersession_count"])
        previous_report_counters = (
            int(report["lifetime_processed_pairs"]),
            int(report["lifetime_attempts"]),
            int(report["supersession_count"]),
        )
        for _ in range(16):
            clock[0] = max(
                clock[0] + timedelta(seconds=120),
                datetime.fromisoformat(str(churn_row["available_at"]))
                + timedelta(seconds=1),
            )
            result = relation_rebuild_queue.drain_relation_rebuild_queue(
                conn,
                max_events=1,
                pair_limit=4,
                worker_id="continuous-churn-worker",
            )
            row = conn.execute(
                """
                SELECT status, available_at, processed_pairs, attempts,
                       supersession_count, pass_number
                FROM relation_rebuild_queue
                WHERE scope_id='scope-live' AND focus_memory_id='focus'
                """
            ).fetchone()
            current_processed = int(row["processed_pairs"])
            assert current_processed >= previous_processed
            assert current_processed - previous_processed <= 1
            assert int(row["attempts"]) >= previous_attempts
            assert int(row["supersession_count"]) >= previous_supersessions
            previous_processed = current_processed
            previous_attempts = int(row["attempts"])
            previous_supersessions = int(row["supersession_count"])
            report = relation_rebuild_queue.relation_rebuild_queue_report(conn)
            report_counters = (
                int(report["lifetime_processed_pairs"]),
                int(report["lifetime_attempts"]),
                int(report["supersession_count"]),
            )
            assert all(
                current >= previous
                for current, previous in zip(
                    report_counters,
                    previous_report_counters,
                    strict=True,
                )
            )
            previous_report_counters = report_counters
            churn_row = row
            if str(row["status"]) == "completed":
                assert result["events_completed"] == 1
                break
            if deferred_write:
                deferred_write = False
                change_corpus()
        else:
            pytest.fail("relation rebuild queue did not converge during sustained corpus churn")

        assert previous_processed >= 4
        assert writes >= 2
        assert blocked_writes >= 1
        assert previous_supersessions >= 2
        assert int(churn_row["pass_number"]) >= 1
        assert report["status"] == "ready"
        assert report["unresolved"] == 0
        assert report["lifetime_processed_pairs"] == previous_processed
        assert report["lifetime_attempts"] == previous_attempts
        assert report["supersession_count"] == previous_supersessions
    finally:
        conn.close()
        writer.close()
