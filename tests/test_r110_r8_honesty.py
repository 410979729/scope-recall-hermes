"""R8 decisive honesty: doctor aggregate, durable cursor receipt, preflight IDs.

These nodes first prove the post-R7 gaps, then stay as the integration
vertical after the synthesized production change.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from journal_source_restore_support import (
    apply_kwargs,
    build_source_restore_pair,
    checkpoint_sqlite_file,
    open_fixture_connection,
    rebind_source_expectations,
)
from scope_recall.journal_source_restore import (
    _empty_receipt,
    _receipt_from_ledger_row,
    run_journal_source_restore,
)
from scope_recall.journal_store import upsert_session_digest_state
from scope_recall.maintenance_lease import acquire_activation_lease, release_activation_lease
from test_r110_final_integration import _home


def _run_restore(**kwargs):
    return run_journal_source_restore(**kwargs)


def _apply_restore(pair, **overrides):
    lease = acquire_activation_lease(pair.target_path)
    try:
        kwargs = apply_kwargs(pair)
        kwargs.update(overrides)
        return _run_restore(**kwargs)
    finally:
        release_activation_lease(lease)


def _journal_doctor_home(
    tmp_path: Path,
    *,
    include_defer_count: bool,
    include_retryable_failures: bool,
) -> Path:
    """Minimal journal schema that can omit either budget column independently."""

    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    extra_columns = []
    if include_defer_count:
        extra_columns.append("defer_count INTEGER")
    if include_retryable_failures:
        extra_columns.append("retryable_failures INTEGER")
    extras = ",\n            ".join(extra_columns)
    extra_sql = f",\n            {extras}" if extras else ""
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.executescript(
        f"""
        CREATE TABLE journal_entries (
            id INTEGER PRIMARY KEY,
            scope_id TEXT,
            shared_scope_id TEXT,
            session_id TEXT,
            turn_number INTEGER,
            role TEXT,
            content TEXT,
            content_hash TEXT,
            created_at TEXT,
            processed_run_id TEXT,
            deferred_run_id TEXT,
            deferred_at TEXT{extra_sql}
        );
        CREATE TABLE journal_digest_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            extractor TEXT,
            interval_label TEXT,
            processed_entries INTEGER,
            inserted INTEGER,
            updated INTEGER,
            skipped INTEGER,
            error TEXT,
            metadata TEXT
        );
        CREATE TABLE memory_journal_sources (
            memory_id TEXT,
            journal_entry_id INTEGER,
            run_id TEXT,
            created_at TEXT
        );
        CREATE TABLE journal_rejections (
            journal_entry_id INTEGER,
            run_id TEXT,
            reason TEXT,
            candidate TEXT,
            created_at TEXT
        );
        """
    )
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        """
        INSERT INTO journal_entries(
            id, scope_id, shared_scope_id, session_id, turn_number, role,
            content, content_hash, created_at, processed_run_id, deferred_run_id
        ) VALUES (1, 's', 'sh', 'sess', 1, 'user', 'old', 'ab', ?, '', '')
        """,
        (created_at,),
    )
    conn.commit()
    conn.close()
    return hermes_home


def _assert_schema_recommendations_are_operator_safe(recommendations: list[str]) -> None:
    joined = " ".join(recommendations)
    assert "migrat" in joined.lower()
    assert "secret" not in joined.lower()
    assert "SELECT" not in joined
    assert "ALTER TABLE" not in joined
    assert len(recommendations) == len(set(recommendations))


def test_doctor_missing_retryable_only_makes_digest_health_unknown(tmp_path):
    from scope_recall.doctor_journal import journal_report

    hermes_home = _journal_doctor_home(
        tmp_path, include_defer_count=True, include_retryable_failures=False
    )
    payload, check, recommendations = journal_report(hermes_home)
    deferred = payload["backlog"]["deferred"]
    retryable = payload["backlog"]["retryable_failures"]
    assert deferred.get("available") is True
    assert retryable.get("available") is False
    assert retryable.get("pending_entries") is None
    assert payload["status"] == "ready"
    assert check["ok"] is True
    assert payload["digest_health"]["status"] != "ready"
    assert payload["digest_health"]["status"] == "unknown"
    assert "retryable_schema_unavailable" in payload["digest_health"]["reasons"]
    assert "deferred_schema_unavailable" not in payload["digest_health"]["reasons"]
    _assert_schema_recommendations_are_operator_safe(recommendations)


def test_doctor_missing_defer_only_keeps_deferred_schema_unavailable(tmp_path):
    from scope_recall.doctor_journal import journal_report

    hermes_home = _journal_doctor_home(
        tmp_path, include_defer_count=False, include_retryable_failures=True
    )
    payload, check, recommendations = journal_report(hermes_home)
    deferred = payload["backlog"]["deferred"]
    retryable = payload["backlog"]["retryable_failures"]
    assert deferred.get("available") is False
    assert retryable.get("available") is True
    assert retryable.get("pending_entries") == 0
    assert payload["status"] == "ready"
    assert check["ok"] is True
    assert payload["digest_health"]["status"] != "ready"
    assert "deferred_schema_unavailable" in payload["digest_health"]["reasons"]
    assert "retryable_schema_unavailable" not in payload["digest_health"]["reasons"]
    _assert_schema_recommendations_are_operator_safe(recommendations)


def test_doctor_both_budget_columns_missing_reports_both_reasons(tmp_path):
    from scope_recall.doctor_journal import journal_report

    hermes_home = _journal_doctor_home(
        tmp_path, include_defer_count=False, include_retryable_failures=False
    )
    payload, check, recommendations = journal_report(hermes_home)
    assert payload["backlog"]["deferred"].get("available") is False
    assert payload["backlog"]["retryable_failures"].get("available") is False
    assert payload["status"] == "ready"
    assert check["ok"] is True
    reasons = payload["digest_health"]["reasons"]
    assert payload["digest_health"]["status"] != "ready"
    assert "deferred_schema_unavailable" in reasons
    assert "retryable_schema_unavailable" in reasons
    assert reasons.count("deferred_schema_unavailable") == 1
    assert reasons.count("retryable_schema_unavailable") == 1
    _assert_schema_recommendations_are_operator_safe(recommendations)


def test_doctor_current_schema_true_zero_keeps_digest_health_ready(tmp_path):
    from scope_recall.doctor_journal import journal_report

    hermes_home, conn = _home(tmp_path, {})
    conn.close()
    payload, check, _recommendations = journal_report(hermes_home)
    assert check["ok"] is True
    assert payload["status"] == "ready"
    assert payload["backlog"]["deferred"].get("available") is True
    assert payload["backlog"]["deferred"].get("count") == 0
    assert payload["backlog"]["retryable_failures"].get("available") is True
    assert payload["backlog"]["retryable_failures"].get("pending_entries") == 0
    assert payload["digest_health"]["status"] == "ready"
    assert "deferred_schema_unavailable" not in payload["digest_health"]["reasons"]
    assert "retryable_schema_unavailable" not in payload["digest_health"]["reasons"]


def test_restore_cursor_reset_survives_ledger_and_replay(tmp_path):
    pair = build_source_restore_pair(tmp_path, occupy_source_ids=True)
    conn = open_fixture_connection(pair.target_path)
    try:
        upsert_session_digest_state(
            conn,
            scope_id="scope-approved",
            session_id="session-09",
            resume_after_id=9000,
            run_id="pre-unprocessed",
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    pair = rebind_source_expectations(pair)

    first = _apply_restore(pair, operation_id="op_jsr_r8_cursor")
    assert first["ok"] is True
    assert int(first["cursor_reset_count"]) >= 1
    assert first.get("cursor_reset_digest")
    first_count = int(first["cursor_reset_count"])
    first_digest = str(first["cursor_reset_digest"])
    dumped = json.dumps(first, ensure_ascii=False)
    assert "scope-approved" not in dumped
    assert "session-09" not in dumped
    assert "secret" not in dumped.lower()
    assert "scope-approved" not in first_digest
    assert "session-09" not in first_digest

    conn = open_fixture_connection(pair.target_path)
    try:
        row = conn.execute(
            "SELECT result_json FROM operator_operations WHERE operation_id = ?",
            ("op_jsr_r8_cursor",),
        ).fetchone()
    finally:
        conn.close()
    result = json.loads(str(row["result_json"]))
    assert int(result["cursor_reset_count"]) == first_count
    assert str(result["cursor_reset_digest"]) == first_digest
    assert result["cursor_reset_digest"] == first_digest
    assert "scope-approved" not in json.dumps(result, ensure_ascii=False)
    assert "session-09" not in json.dumps(result, ensure_ascii=False)

    replay = _apply_restore(pair, operation_id="op_jsr_r8_cursor")
    assert replay["ok"] is True
    assert replay["verdict"] in {"applied", "committed_reconciled"}
    assert int(replay["cursor_reset_count"]) == first_count
    assert str(replay["cursor_reset_digest"]) == first_digest

    from test_journal_source_restore_ledger import _pin_wal_visible_sidecars

    pin = _pin_wal_visible_sidecars(pair.target_path)
    try:
        wal_replay = _apply_restore(pair, operation_id="op_jsr_r8_cursor")
    finally:
        pin.close()
    assert wal_replay["ok"] is True
    assert wal_replay["verdict"] == "committed_reconciled"
    assert int(wal_replay["cursor_reset_count"]) == first_count
    assert str(wal_replay["cursor_reset_digest"]) == first_digest


def test_old_ledger_row_without_cursor_fields_defaults_safely():
    receipt = _receipt_from_ledger_row(
        {
            "operation_id": "op_legacy",
            "request_fingerprint": "f" * 64,
            "receipt_state": "pending",
            "result_json": json.dumps(
                {
                    "journal_inserted_count": 3,
                    "mapping_count": 3,
                    "mapping_digest": "a" * 64,
                    "verdict": "applied",
                }
            ),
        },
        receipt=_empty_receipt(dry_run=False),
    )
    assert int(receipt["cursor_reset_count"]) == 0
    assert str(receipt["cursor_reset_digest"] or "") == ""
    assert receipt["operation_id"] == "op_legacy"


_UNSAFE_READY_GENERATION_IDS = (
    "sk-" + "A" * 24,
    r"C:\Users\synthetic\AppData\Local\Temp\screenshot.png",
    "/home/synthetic/.hermes/image_cache/img_posix.png",
    r"\\fileserver\share\screenshots\shot.png",
    "[Image attached at: C:\\Users\\synthetic\\shot.png]",
    "[screenshot]",
)


def _relabel_ready_generation(storage, conn, old_id: str, new_id: str) -> None:
    """Keep the physical store, rewrite only the public generation identity."""

    from scope_recall.vector_generation import canonical_json_hash
    from scope_recall.vector_generation_preflight import PREFLIGHT_RECEIPT_FILENAME

    target = storage / "vector-generations" / old_id
    receipt_path = target / PREFLIGHT_RECEIPT_FILENAME
    body = json.loads(receipt_path.read_text(encoding="utf-8"))
    body["generation_id"] = new_id
    unsigned = {key: value for key, value in body.items() if key != "receipt_sha256"}
    body["receipt_sha256"] = canonical_json_hash(unsigned)
    receipt_path.write_text(
        json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    conn.execute(
        "UPDATE vector_generations SET generation_id = ? WHERE generation_id = ?",
        (new_id, old_id),
    )
    conn.commit()


def _expected_redacted_generation_id(raw: str) -> str:
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:16]
    return f"redacted-generation-{digest}"


def test_healthy_ready_preflight_and_doctor_hide_raw_generation_ids(tmp_path):
    import test_vector_generation_migration as vgm
    from scope_recall.vector_generation import generation_manifest
    from scope_recall.vector_generation_preflight import validate_generation_physical_store

    storage, conn, identity, old = vgm._sqlite_fixture(tmp_path)
    active_id = str(old["generation_id"])
    store_ids = [f"gen-r8-store-{index:02d}" for index in range(len(_UNSAFE_READY_GENERATION_IDS))]
    for store_id, raw_id in zip(store_ids, _UNSAFE_READY_GENERATION_IDS, strict=True):
        vgm._build_sqlite_ready(storage, conn, identity, old, store_id)
        _relabel_ready_generation(storage, conn, store_id, raw_id)

    token_id = _UNSAFE_READY_GENERATION_IDS[0]
    report = validate_generation_physical_store(
        storage,
        generation_manifest(conn, token_id),
        require_receipt=True,
    )
    assert report["ok"] is True
    report_dump = json.dumps(report, ensure_ascii=False)
    assert token_id not in report_dump
    assert report["generation_id"] == _expected_redacted_generation_id(token_id)
    conn.close()

    payload, check, _recommendations = vgm._doctor_generation_report(tmp_path, identity)
    assert check["ok"] is True
    dumped = json.dumps(payload, ensure_ascii=False)
    for raw in _UNSAFE_READY_GENERATION_IDS:
        assert raw not in dumped
    assert active_id not in {
        str(item.get("generation_id") or "")
        for item in payload["inactive_generation_inventory"]
    }
    inventory = vgm._inventory_by_id(payload)
    assert len(inventory) == len(_UNSAFE_READY_GENERATION_IDS)
    assert active_id not in inventory
    outward_ids = [_expected_redacted_generation_id(raw) for raw in _UNSAFE_READY_GENERATION_IDS]
    assert len(set(outward_ids)) == len(outward_ids)
    assert set(inventory) == set(outward_ids)
    preflight_ids = {
        str(item.get("generation_id") or "")
        for item in payload["ready_generation_preflights"]
    }
    assert preflight_ids == set(outward_ids)
    assert all(item.get("ok") is True for item in payload["ready_generation_preflights"])
    assert payload["ready_generation_preflight_failures"] == []
    assert payload["rebuild_from_sqlite_required"] is False
    for item in payload["ready_generation_preflights"]:
        assert str(item["generation_id"]).startswith("redacted-generation-")


def test_redacted_generation_identifiers_stay_distinct_and_stable():
    from scope_recall.doctor_vector import _inactive_ready_inventory_item

    first = r"C:\Users\synthetic\AppData\Local\Temp\shot-a.png"
    second = r"C:\Users\synthetic\AppData\Local\Temp\shot-b.png"
    token = "sk-" + "B" * 24
    left = _expected_redacted_generation_id(first)
    right = _expected_redacted_generation_id(second)
    token_id = _expected_redacted_generation_id(token)
    items = [
        _inactive_ready_inventory_item(generation_id=first, activatable=True),
        _inactive_ready_inventory_item(generation_id=second, activatable=True),
        _inactive_ready_inventory_item(generation_id=first, activatable=True),
        _inactive_ready_inventory_item(generation_id=token, activatable=True),
    ]
    assert items[0]["generation_id"] == left
    assert items[1]["generation_id"] == right
    assert items[2]["generation_id"] == left
    assert items[3]["generation_id"] == token_id
    assert items[0]["generation_id"] != items[1]["generation_id"]
    dumped = json.dumps(items, ensure_ascii=False)
    assert first not in dumped
    assert second not in dumped
    assert token not in dumped
    assert "[REDACTED_PATH]" not in dumped
    assert "[REDACTED_SECRET]" not in dumped
