"""The common error writer preserves each caller's existing row and retention semantics."""

import sqlite3
from types import SimpleNamespace

import pytest
from scope_recall.core.failure_retry import validation_feedback
from scope_recall.core.injection_guard import record_scan_deadline
from scope_recall.core.worker_outcomes import record_error_detail


@pytest.mark.parametrize(
    ("stage", "code", "field", "retain"),
    [
        ("candidate_evaluation", "INPUT_INVALID", None, False),
        ("summary", "DERIVATION_INVALID", "fragment.summary", False),
        ("claim", "DERIVATION_INVALID", "claim[0].quote", False),
        ("summary", "INPUT_INVALID", "summary_field", False),
        ("source_scan", "scan_time_budget", None, False),
        ("entity", "entity_gap", None, False),
        ("process", "DERIVATION_INVALID", validation_feedback("DERIVATION_INVALID", "bad field!")["field"], True),
        ("summary", "resume_hint_omitted", "resume_hint_omitted_input_budget", False),
    ],
)
def test_error_rows_match_prior_writers(stage, code, field, retain):
    connections = [sqlite3.connect(":memory:") for _ in range(2)]
    for conn in connections:
        conn.execute(
            "CREATE TABLE work_error_details (detail_id INTEGER PRIMARY KEY, work_id INTEGER, lease_token INTEGER, "
            "stage TEXT, error_code TEXT, error_field TEXT, recorded_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO work_error_details(work_id, lease_token, stage, error_code, error_field, recorded_at) "
            "VALUES (1, 7, 'old', 'old', NULL, ?)",
            [(str(index),) for index in range(18)],
        )
        conn.execute("INSERT INTO work_error_details VALUES (NULL, 2, 9, 'other', 'other', NULL, 'before')")
    old, new = connections
    old.execute(
        "INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) "
        "VALUES (?,?,?,?,?,?)",
        (1, 7, stage, code, field, "now"),
    )
    if retain:
        old.execute(
            "DELETE FROM work_error_details WHERE work_id=? AND detail_id NOT IN "
            "(SELECT detail_id FROM work_error_details WHERE work_id=? ORDER BY detail_id DESC LIMIT 16)",
            (1, 1),
        )
    record_error_detail(new, 1, 7, now="now", stage=stage, code=code, field=field, retain=retain)
    assert (
        old.execute("SELECT * FROM work_error_details ORDER BY detail_id").fetchall()
        == new.execute("SELECT * FROM work_error_details ORDER BY detail_id").fetchall()
    )
    assert new.execute("SELECT count(*) FROM work_error_details WHERE work_id=1").fetchone()[0] == (
        16 if retain else 19
    )
    for conn in connections:
        conn.close()


def test_scan_deadline_records_each_matching_work_item():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE work_error_details (detail_id INTEGER PRIMARY KEY, work_id INTEGER, lease_token INTEGER, "
        "stage TEXT, error_code TEXT, error_field TEXT, recorded_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE work_items (work_id INTEGER, lease_token INTEGER, work_type TEXT, "
        "subject_ref TEXT, subject_revision INTEGER)"
    )
    conn.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?)",
        [
            (1, 7, "rebuild_projection", "source", 1),
            (2, 8, "rebuild_projection", "source", 1),
            (3, 9, "rebuild_projection", "source", 2),
            (4, 10, "other", "source", 1),
        ],
    )
    tx = SimpleNamespace(_check=lambda **_: conn)
    record_scan_deadline(
        tx, [{"event_id": "source", "source_revision": 1}], SimpleNamespace(rules={"scan_time_budget"}), "now"
    )
    assert conn.execute("SELECT * FROM work_error_details ORDER BY detail_id").fetchall() == [
        (1, 1, 7, "source_scan", "scan_time_budget", None, "now"),
        (2, 2, 8, "source_scan", "scan_time_budget", None, "now"),
    ]
    conn.close()
