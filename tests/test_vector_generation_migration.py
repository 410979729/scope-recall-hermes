"""Failure-atomic tests for explicit shadow vector generation builds."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scope_recall.embedders import LocalHashEmbedder
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
from scope_recall.vector_generation import (
    GenerationCompatibilityError,
    GenerationIdentity,
    bootstrap_legacy_generation,
    current_generation,
    enqueue_vector_event,
    generation_manifest,
    register_generation,
)
from scope_recall.vector_migration import (
    _validate_shadow_records,
    build_vector_generation,
    plan_vector_generation,
)
from scope_recall.vector_generation_preflight import (
    PREFLIGHT_RECEIPT_FILENAME,
    physical_records_sha256,
    validate_generation_physical_store,
)
from scope_recall.vector_membership import membership_is_ready
from scope_recall.vector_reconciliation import vector_reconciliation_state
from scope_recall.vector_store import LanceVectorStore


def _record_validation_fixture():
    identity = GenerationIdentity(
        backend="lancedb",
        provider="local-hash",
        model="hash-v1",
        dimensions=3,
    )
    source = {
        "id": "memory-contract",
        "scope_id": "scope-a",
        "source": "fixture",
        "target": "memory",
        "content": "contract content",
        "summary": "contract summary",
        "updated_at": "2026-07-10T00:00:00+00:00",
    }
    record = {**source, "vector": [0.1, 0.2, 0.3]}
    return identity, source, record


def test_shadow_record_validation_accepts_exact_complete_record():
    identity, source, record = _record_validation_fixture()
    vector, scope_id = _validate_shadow_records(
        {str(record["id"]): record},
        [source],
        identity,
    )
    assert vector == [0.1, 0.2, 0.3]
    assert scope_id == "scope-a"


def test_physical_record_digest_is_order_independent_and_content_complete():
    _identity, _source, first = _record_validation_fixture()
    second = {**first, "id": "memory-z", "vector": [0.3, 0.2, 0.1]}
    forward = {str(first["id"]): first, str(second["id"]): second}
    reverse = {str(second["id"]): second, str(first["id"]): first}
    baseline = physical_records_sha256(forward)

    assert physical_records_sha256(reverse) == baseline
    assert physical_records_sha256(
        {**forward, str(first["id"]): {**first, "content": "changed"}}
    ) != baseline
    assert physical_records_sha256(
        {**forward, str(first["id"]): {**first, "vector": [0.1, 0.2, 0.4]}}
    ) != baseline


def test_shadow_record_validation_rejects_content_vector_and_id_corruption():
    identity, source, record = _record_validation_fixture()
    bad_cases = [
        ("ID set", {}, "id"),
        ("content", {"memory-contract": {**record, "content": "wrong"}}, "content"),
        ("dimensions", {"memory-contract": {**record, "vector": [0.1, 0.2]}}, "dimensions"),
        ("non-finite", {"memory-contract": {**record, "vector": [0.1, float("nan"), 0.3]}}, "non-finite"),
        ("zero", {"memory-contract": {**record, "vector": [0.0, 0.0, 0.0]}}, "zero"),
    ]
    for _label, records, pattern in bad_cases:
        with pytest.raises(RuntimeError, match=pattern):
            _validate_shadow_records(records, [source], identity)


def _fixture(tmp_path: Path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for index in range(3):
        store_row(
            conn,
            memory_id=f"memory-{index}",
            scope_id="scope-a",
            platform="test",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="fixture",
            target="memory",
            content=f"shadow generation fixture {index}",
            allow_duplicate=True,
        )
    identity = GenerationIdentity(
        backend="lancedb",
        provider="local-hash",
        model="hash-v1",
        dimensions=16,
        metric="cosine",
        prompt_profile="default-v1",
        table_name="memories",
    )
    old = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    conn.commit()
    return storage, conn, identity, old


def _sqlite_fixture(tmp_path: Path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for index in range(3):
        store_row(
            conn,
            memory_id=f"memory-{index}",
            scope_id="scope-a",
            platform="test",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="fixture",
            target="memory",
            content=f"physical preflight fixture {index}",
            allow_duplicate=True,
        )
    identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=16,
        metric="cosine",
        prompt_profile="default-v1",
        table_name="memories",
    )
    old = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    conn.commit()
    return storage, conn, identity, old


def _build_sqlite_ready(storage, conn, identity, old, generation_id):
    result = build_vector_generation(
        storage,
        conn,
        generation_id=generation_id,
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
        index_general=False,
        activate=False,
        expected_current=old["generation_id"],
    )
    assert result["status"] == "ready"
    return storage / "vector-generations" / generation_id


def test_shadow_build_seeds_authoritative_generation_membership(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)

    _build_sqlite_ready(storage, conn, identity, old, "gen-membership")

    assert membership_is_ready(conn, "gen-membership") is True
    membership_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT memory_id FROM vector_id_membership WHERE generation_id = ? ORDER BY memory_id",
            ("gen-membership",),
        ).fetchall()
    ]
    assert membership_ids == ["memory-0", "memory-1", "memory-2"]
    conn.close()


def _activate_existing_ready(storage, conn, identity, old, generation_id):
    return build_vector_generation(
        storage,
        conn,
        generation_id=generation_id,
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
        index_general=False,
        activate=True,
        expected_current=old["generation_id"],
        activate_existing_ready=True,
    )


def test_activation_refuses_ready_generation_when_physical_storage_is_missing(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-missing-store")
    shutil.rmtree(target)

    with pytest.raises(GenerationCompatibilityError, match="physical|storage|missing"):
        _activate_existing_ready(storage, conn, identity, old, "gen-missing-store")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-missing-store")["status"] == "ready"
    assert not target.exists()
    conn.close()


def test_activation_refuses_ready_generation_when_physical_storage_is_corrupt(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-corrupt-store")
    (target / "vector.sqlite3").write_bytes(b"not-a-sqlite-database")

    with pytest.raises(GenerationCompatibilityError, match="physical|storage|corrupt|database"):
        _activate_existing_ready(storage, conn, identity, old, "gen-corrupt-store")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-corrupt-store")["status"] == "ready"
    conn.close()


def test_activation_refuses_ready_generation_with_wrong_physical_identity(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-wrong-identity")
    vector_conn = sqlite3.connect(target / "vector.sqlite3")
    vector_conn.execute("UPDATE vector_meta SET value = ? WHERE key = 'dimensions'", ("8",))
    vector_conn.commit()
    vector_conn.close()

    with pytest.raises(GenerationCompatibilityError, match="identity|dimension|physical"):
        _activate_existing_ready(storage, conn, identity, old, "gen-wrong-identity")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-wrong-identity")["status"] == "ready"
    conn.close()


def test_activation_refuses_ready_generation_with_physical_row_mismatch(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-row-mismatch")
    vector_db = sqlite3.connect(target / "vector.sqlite3")
    vector_db.execute("DELETE FROM vector_records WHERE id = (SELECT id FROM vector_records LIMIT 1)")
    vector_db.commit()
    vector_db.close()

    with pytest.raises(GenerationCompatibilityError, match="row|count|physical"):
        _activate_existing_ready(storage, conn, identity, old, "gen-row-mismatch")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-row-mismatch")["status"] == "ready"
    conn.close()


def test_activation_refuses_same_count_physical_record_tamper(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-same-count-tamper")
    vector_db = sqlite3.connect(target / "vector.sqlite3")
    vector_db.execute(
        "UPDATE vector_records SET content = content || ? WHERE id = (SELECT id FROM vector_records LIMIT 1)",
        (" [tampered]",),
    )
    vector_db.commit()
    vector_db.close()

    with pytest.raises(GenerationCompatibilityError, match="receipt|record|physical|hash"):
        _activate_existing_ready(storage, conn, identity, old, "gen-same-count-tamper")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-same-count-tamper")["status"] == "ready"
    conn.close()


def test_activation_refuses_ready_generation_without_bound_preflight_receipt(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-no-preflight-receipt")
    (target / PREFLIGHT_RECEIPT_FILENAME).unlink()

    with pytest.raises(GenerationCompatibilityError, match="receipt|preflight|missing"):
        _activate_existing_ready(storage, conn, identity, old, "gen-no-preflight-receipt")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-no-preflight-receipt")["status"] == "ready"
    conn.close()


def test_ready_generation_cannot_bypass_receipt_by_forging_activated_at(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-forged-activated-at")
    (target / PREFLIGHT_RECEIPT_FILENAME).unlink()
    conn.execute(
        "UPDATE vector_generations SET activated_at = ? WHERE generation_id = ?",
        ("2026-07-12T00:00:00+00:00", "gen-forged-activated-at"),
    )
    conn.commit()

    with pytest.raises(GenerationCompatibilityError, match="receipt|preflight|missing"):
        _activate_existing_ready(storage, conn, identity, old, "gen-forged-activated-at")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-forged-activated-at")["status"] == "ready"
    conn.close()


def test_activation_rolls_back_pointer_when_status_transition_fails(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    _build_sqlite_ready(storage, conn, identity, old, "gen-status-failure")
    conn.execute(
        """
        CREATE TRIGGER reject_generation_activation
        BEFORE UPDATE OF status ON vector_generations
        WHEN NEW.generation_id = 'gen-status-failure' AND NEW.status = 'active'
        BEGIN
            SELECT RAISE(ABORT, 'injected activation status failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.DatabaseError, match="injected activation status failure"):
        _activate_existing_ready(storage, conn, identity, old, "gen-status-failure")
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-status-failure")["status"] == "ready"
    assert generation_manifest(conn, old["generation_id"])["status"] == "active"
    assert conn.in_transaction is False
    conn.close()


def test_generation_physical_preflight_is_read_only_for_existing_sqlite_store(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-read-only-preflight")
    vector_path = target / "vector.sqlite3"
    receipt_path = target / PREFLIGHT_RECEIPT_FILENAME
    before = (vector_path.stat().st_mtime_ns, receipt_path.stat().st_mtime_ns)

    report = validate_generation_physical_store(
        storage,
        generation_manifest(conn, "gen-read-only-preflight"),
        require_receipt=True,
    )

    assert report["physical_rows"] == 3
    assert (vector_path.stat().st_mtime_ns, receipt_path.stat().st_mtime_ns) == before
    conn.close()


def _expected_embedder(identity):
    return {
        "provider": identity.provider,
        "model": identity.model,
        "dimensions": identity.dimensions,
        "metric": identity.metric,
        "prompt_profile": identity.prompt_profile,
        "document_prefix": identity.document_prefix,
        "query_prefix": identity.query_prefix,
        "request_dimensions": identity.request_dimensions,
        "available": True,
    }


def _doctor_generation_report(tmp_path, identity):
    from scope_recall.doctor_vector import vector_generation_report

    return vector_generation_report(
        tmp_path,
        expected_embedder=_expected_embedder(identity),
        backend=identity.backend,
    )


_INACTIVE_INVENTORY_FIELDS = {
    "generation_id",
    "activatable",
    "label",
    "rebuild_from_sqlite_required",
    "reason",
    "repair",
}


def _inventory_by_id(payload):
    items = list(payload["inactive_generation_inventory"])
    ids = [str(item.get("generation_id") or "") for item in items]
    assert ids == sorted(ids)
    assert all(_INACTIVE_INVENTORY_FIELDS <= set(item) for item in items)
    return {str(item["generation_id"]): item for item in items}


def test_doctor_reports_invalid_inactive_ready_generation_without_failing_active_health(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    active_id = str(old["generation_id"])
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-doctor-missing")
    shutil.rmtree(target)
    enqueue_vector_event(
        conn,
        event_key="inactive-ready-debt",
        generation_id="gen-doctor-missing",
        memory_id="memory-0",
        operation="upsert",
        payload={"reason": "inactive fixture debt"},
    )
    conn.commit()
    conn.close()

    payload, check, recommendations = _doctor_generation_report(tmp_path, identity)
    assert check["ok"] is True
    assert payload["status"] == "ready"
    assert payload["current_generation_id"] == active_id
    assert payload["ready_generation_preflight_failures"]
    assert payload["rebuild_from_sqlite_required"] is True
    inventory = _inventory_by_id(payload)
    assert set(inventory) == {"gen-doctor-missing"}
    assert active_id not in inventory
    assert inventory["gen-doctor-missing"]["rebuild_from_sqlite_required"] is True
    assert payload["outbox_backlog"] == 0
    assert payload["inactive_outbox_status_counts"]["pending"] == 1
    assert any("cannot be activated" in item.lower() for item in recommendations)


def test_doctor_inactive_generation_inventory_fields_for_healthy_and_broken_ready(tmp_path):
    from scope_recall.capture_filters import sanitize_report_text

    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    active_id = str(old["generation_id"])
    _build_sqlite_ready(storage, conn, identity, old, "gen-doctor-healthy-sibling")
    target = _build_sqlite_ready(storage, conn, identity, old, "gen-doctor-sidecar")
    (target / "vector.sqlite3-wal").write_bytes(b"stale-wal")
    (target / "vector.sqlite3-shm").write_bytes(b"stale-shm")
    conn.close()

    payload, check, recommendations = _doctor_generation_report(tmp_path, identity)
    assert check["ok"] is True
    assert payload["status"] == "ready"
    assert payload["current_generation_id"] == active_id
    assert payload["rebuild_from_sqlite_required"] is True
    failures = payload["ready_generation_preflight_failures"]
    assert [item["generation_id"] for item in failures] == ["gen-doctor-sidecar"]
    assert failures[0]["error"]
    assert failures[0].get("activatable") is False
    assert failures[0].get("label") == "non_activatable"
    inventory = _inventory_by_id(payload)
    assert list(inventory) == ["gen-doctor-healthy-sibling", "gen-doctor-sidecar"]
    assert active_id not in inventory

    healthy = inventory["gen-doctor-healthy-sibling"]
    assert healthy["activatable"] is True
    assert healthy["label"] == "activatable"
    assert healthy["rebuild_from_sqlite_required"] is False
    assert healthy["reason"] == ""
    assert healthy["repair"] == ""

    broken = inventory["gen-doctor-sidecar"]
    assert broken["activatable"] is False
    assert broken["label"] == "non_activatable"
    assert broken["rebuild_from_sqlite_required"] is True
    assert broken["reason"] == sanitize_report_text(broken["reason"])[:300]
    assert broken["reason"] == failures[0]["error"]
    assert len(broken["reason"]) <= 300
    assert "sidecar" in broken["reason"].lower()
    assert "sqlite" in broken["repair"].lower()
    assert len(broken["repair"]) <= 300
    assert "vector.sqlite3" not in broken["repair"]
    assert str(target) not in broken["reason"]
    assert str(target) not in broken["repair"]
    assert any("cannot be activated" in item.lower() for item in recommendations)

    preflights = payload["ready_generation_preflights"]
    assert [item["generation_id"] for item in preflights] == ["gen-doctor-healthy-sibling"]
    assert preflights[0].get("ok") is True
    assert preflights[0].get("activatable") is True
    assert preflights[0].get("label") == "activatable"
    assert "gen-doctor-sidecar" not in {
        str(item.get("generation_id") or "") for item in preflights
    }


def test_doctor_payload_does_not_misclassify_healthy_inactive_ready(tmp_path):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    active_id = str(old["generation_id"])
    _build_sqlite_ready(storage, conn, identity, old, "gen-doctor-healthy")
    conn.close()

    payload, check, recommendations = _doctor_generation_report(tmp_path, identity)
    assert check["ok"] is True
    assert payload["status"] == "ready"
    assert payload["current_generation_id"] == active_id
    assert payload["ready_generation_preflight_failures"] == []
    assert payload["rebuild_from_sqlite_required"] is False
    inventory = _inventory_by_id(payload)
    assert list(inventory) == ["gen-doctor-healthy"]
    assert active_id not in inventory
    healthy = inventory["gen-doctor-healthy"]
    assert healthy["activatable"] is True
    assert healthy["label"] == "activatable"
    assert healthy["rebuild_from_sqlite_required"] is False
    assert healthy["reason"] == ""
    assert healthy["repair"] == ""
    preflights = payload["ready_generation_preflights"]
    assert [item.get("generation_id") for item in preflights] == ["gen-doctor-healthy"]
    assert all(item.get("ok") is True for item in preflights)
    assert all(item.get("activatable") is True for item in preflights)
    assert all(item.get("label") == "activatable" for item in preflights)
    assert not any(item.get("rebuild_from_sqlite_required") for item in inventory.values())
    assert not any("cannot be activated" in item.lower() for item in recommendations)


def test_doctor_inactive_generation_inventory_reason_is_sanitized(tmp_path, monkeypatch):
    storage, conn, identity, old = _sqlite_fixture(tmp_path)
    _build_sqlite_ready(storage, conn, identity, old, "gen-doctor-secret")
    conn.close()

    reserved_path = "/home/" + "synthetic-operator/.hermes/reserved-secret.db"

    def _boom(*_args, **_kwargs):
        raise RuntimeError(
            "token=abcdefghijklmnopqrstuvwxyz path=" + reserved_path
        )

    monkeypatch.setattr(
        "scope_recall.doctor_vector.validate_generation_for_activation",
        _boom,
    )
    payload, check, _recommendations = _doctor_generation_report(tmp_path, identity)
    assert check["ok"] is True
    assert payload["rebuild_from_sqlite_required"] is True
    inventory = _inventory_by_id(payload)
    entry = inventory["gen-doctor-secret"]
    failure = payload["ready_generation_preflight_failures"][0]
    assert entry["generation_id"] == "gen-doctor-secret"
    assert entry["activatable"] is False
    assert entry["label"] == "non_activatable"
    assert entry["rebuild_from_sqlite_required"] is True
    assert entry["reason"] == failure["error"]
    assert "abcdefghijklmnopqrstuvwxyz" not in entry["reason"]
    assert "abcdefghijklmnopqrstuvwxyz" not in entry["repair"]
    assert reserved_path not in entry["reason"]
    assert reserved_path not in entry["repair"]
    assert "/home/synthetic-operator" not in entry["reason"]
    assert "/home/synthetic-operator" not in entry["repair"]
    assert "[REDACTED_PATH]" in entry["reason"]
    assert len(entry["reason"]) <= 300
    assert len(entry["repair"]) <= 300


def _doctor_report_without_ready_scan(tmp_path):
    from scope_recall.doctor_vector import vector_generation_report

    return vector_generation_report(
        tmp_path,
        expected_embedder={
            "provider": "local-hash",
            "model": "hash-v1",
            "dimensions": 16,
            "available": True,
        },
        backend="sqlite-bruteforce",
    )


def _remove_and_check_durable_work(
    payload: dict[str, object], *, state: str, reason_code: str
) -> None:
    durable = payload.pop("durable_work")
    assert isinstance(durable, dict)
    assert durable["schema_version"] == "durable_work.v1"
    assert durable["domain_type"] == "vector_causal_outbox"
    assert durable["state"] == state
    assert durable["reason_code"] == reason_code


def test_doctor_absent_db_exposes_empty_inactive_generation_inventory(tmp_path):
    payload, check, recommendations = _doctor_report_without_ready_scan(tmp_path)
    _remove_and_check_durable_work(
        payload,
        state="disabled",
        reason_code="truth_database_absent",
    )
    assert payload == {
        "status": "absent",
        "registered": False,
        "inactive_generation_inventory": [],
        "rebuild_from_sqlite_required": False,
    }
    assert check == {"ok": True, "failures": []}
    assert recommendations == []


def test_doctor_legacy_unregistered_exposes_empty_inactive_generation_inventory(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    payload, check, recommendations = _doctor_report_without_ready_scan(tmp_path)
    _remove_and_check_durable_work(
        payload,
        state="disabled",
        reason_code="schema_missing",
    )
    assert payload == {
        "status": "legacy_unregistered",
        "registered": False,
        "missing_tables": [
            "vector_generation_state",
            "vector_generations",
            "vector_migration_receipts",
            "vector_outbox",
        ],
        "inactive_generation_inventory": [],
        "rebuild_from_sqlite_required": False,
    }
    assert check == {"ok": True, "failures": []}
    assert any("legacy-unregistered" in item for item in recommendations)


def test_doctor_initialized_unregistered_exposes_empty_inactive_generation_inventory(tmp_path):
    from scope_recall.vector_generation import ensure_vector_generation_schema

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    ensure_vector_generation_schema(conn)
    conn.commit()
    conn.close()

    payload, check, recommendations = _doctor_report_without_ready_scan(tmp_path)
    _remove_and_check_durable_work(
        payload,
        state="disabled",
        reason_code="no_active_generation",
    )
    assert payload == {
        "status": "legacy_unregistered",
        "registered": False,
        "current_generation_id": "",
        "inactive_generation_inventory": [],
        "rebuild_from_sqlite_required": False,
    }
    assert check == {"ok": True, "failures": []}
    assert any("not registered" in item for item in recommendations)


def test_doctor_manifests_without_pointer_require_repair(tmp_path):
    from scope_recall.vector_generation import ensure_vector_generation_schema

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_vector_generation_schema(conn)
    register_generation(
        conn,
        generation_id="orphan-active",
        identity=GenerationIdentity(
            backend="sqlite-bruteforce",
            provider="local-hash",
            model="hash-v1",
            dimensions=8,
        ),
        storage_path=".",
        status="active",
    )
    conn.commit()
    conn.close()

    payload, check, recommendations = _doctor_report_without_ready_scan(tmp_path)

    _remove_and_check_durable_work(
        payload,
        state="disabled",
        reason_code="no_active_generation",
    )

    assert payload == {
        "status": "generation_incomplete",
        "registered": True,
        "current_generation_id": "",
        "orphan_generation_count": 1,
        "inactive_generation_inventory": [],
        "rebuild_from_sqlite_required": False,
    }
    assert check == {
        "ok": False,
        "failures": ["vector generation manifests exist without a current pointer"],
    }
    assert recommendations == [
        "Restore the current generation pointer or CAS-activate a validated READY generation before normal runtime startup."
    ]


def test_doctor_missing_manifest_exposes_empty_inactive_generation_inventory(tmp_path):
    from scope_recall.vector_generation import ensure_vector_generation_schema

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    conn = sqlite3.connect(storage / "memory.sqlite3")
    ensure_vector_generation_schema(conn)
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("current_generation", "gen-missing-manifest", "2026-07-10T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    payload, check, recommendations = _doctor_report_without_ready_scan(tmp_path)
    _remove_and_check_durable_work(
        payload,
        state="disabled",
        reason_code="current_generation_manifest_missing",
    )
    assert payload == {
        "status": "generation_incomplete",
        "registered": True,
        "current_generation_id": "gen-missing-manifest",
        "inactive_generation_inventory": [],
        "rebuild_from_sqlite_required": False,
    }
    assert check == {"ok": False, "failures": ["current vector generation manifest is missing: gen-missing-manifest"]}
    assert recommendations == ["Restore the missing manifest or CAS-activate a validated READY generation."]


def _state(conn: sqlite3.Connection, storage: Path):
    tables = tuple(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
    counts = {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in ("vector_generations", "vector_generation_state", "vector_migration_receipts", "vector_outbox")
    }
    paths = tuple(sorted(str(path.relative_to(storage)) for path in storage.rglob("*") if path.is_file()))
    return tables, counts, paths, current_generation(conn)["generation_id"]


def test_generation_dry_run_is_zero_write(tmp_path):
    storage, conn, identity, _old = _fixture(tmp_path)
    before = _state(conn, storage)

    plan = plan_vector_generation(
        storage,
        conn,
        generation_id="gen-dry-run",
        identity=identity,
        index_general=False,
    )

    assert plan["dry_run"] is True
    assert plan["rows_planned"] == 3
    assert _state(conn, storage) == before
    assert not (storage / "vector-generations" / "gen-dry-run").exists()
    conn.close()


def test_shadow_build_is_ready_without_activation_then_can_activate(tmp_path):
    storage, conn, identity, old = _fixture(tmp_path)
    result = build_vector_generation(
        storage,
        conn,
        generation_id="gen-ready",
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
        index_general=False,
        batch_size=2,
        activate=False,
        expected_current=old["generation_id"],
    )

    assert result["status"] == "ready"
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    manifest = generation_manifest(conn, "gen-ready")
    assert manifest["status"] == "ready"
    assert manifest["row_count"] == 3

    store = LanceVectorStore(
        storage / "vector-generations" / "gen-ready" / "lancedb",
        table_name="memories",
        dimensions=16,
    )
    store.open()
    try:
        assert store.count_rows() == 3
        assert len(set(store.list_ids())) == 3
    finally:
        store.close()

    activated = build_vector_generation(
        storage,
        conn,
        generation_id="gen-ready",
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
        index_general=False,
        activate=True,
        expected_current=old["generation_id"],
        activate_existing_ready=True,
    )
    assert activated["status"] == "activated"
    assert current_generation(conn)["generation_id"] == "gen-ready"
    watermark = vector_reconciliation_state(conn, generation_id="gen-ready")
    assert watermark is not None
    assert watermark["status"] == "idle"
    assert watermark["cursor_updated_at"] == watermark["upper_updated_at"]
    assert watermark["cursor_memory_id"] == watermark["upper_memory_id"]
    assert watermark["processed_rows"] == 3
    assert watermark["enqueued_events"] == 0
    conn.close()


def test_shadow_build_uses_one_immutable_truth_snapshot(tmp_path):
    storage, conn, identity, old = _fixture(tmp_path)
    conn.execute(
        """
        CREATE TRIGGER mutate_truth_after_shadow_manifest
        AFTER INSERT ON vector_generations
        WHEN NEW.generation_id = 'gen-snapshot'
        BEGIN
            UPDATE memories
            SET content = 'mutated after snapshot', updated_at = '2026-07-10T12:00:00+00:00'
            WHERE id = 'memory-0';
        END
        """
    )
    conn.commit()

    result = build_vector_generation(
        storage,
        conn,
        generation_id="gen-snapshot",
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
        index_general=False,
        batch_size=2,
        activate=False,
        expected_current=old["generation_id"],
    )

    assert result["rows_planned"] == result["rows_built"] == 3
    assert conn.execute("SELECT content FROM memories WHERE id = 'memory-0'").fetchone()[0] == "mutated after snapshot"
    store = LanceVectorStore(
        storage / "vector-generations" / "gen-snapshot" / "lancedb",
        table_name="memories",
        dimensions=16,
    )
    store.open()
    try:
        assert store.list_records()["memory-0"]["content"] == "shadow generation fixture 0"
    finally:
        store.close()
    conn.close()


def test_activation_refuses_ready_generation_when_truth_snapshot_is_stale(tmp_path):
    storage, conn, identity, old = _fixture(tmp_path)
    build_vector_generation(
        storage,
        conn,
        generation_id="gen-stale",
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
        index_general=False,
        activate=False,
        expected_current=old["generation_id"],
    )
    conn.execute(
        "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
        ("changed after ready", "2026-07-10T13:00:00+00:00", "memory-1"),
    )
    conn.commit()

    with pytest.raises(GenerationCompatibilityError, match="source snapshot is stale"):
        build_vector_generation(
            storage,
            conn,
            generation_id="gen-stale",
            identity=identity,
            embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
            index_general=False,
            activate=True,
            expected_current=old["generation_id"],
            activate_existing_ready=True,
        )
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-stale")["status"] == "ready"
    conn.close()


def test_direct_build_and_activate_refuses_truth_changed_after_snapshot(tmp_path):
    storage, conn, identity, old = _fixture(tmp_path)
    conn.execute(
        """
        CREATE TRIGGER mutate_truth_before_direct_activation
        AFTER INSERT ON vector_generations
        WHEN NEW.generation_id = 'gen-direct-stale'
        BEGIN
            UPDATE memories
            SET content = 'mutated before direct activation', updated_at = '2026-07-10T14:00:00+00:00'
            WHERE id = 'memory-2';
        END
        """
    )
    conn.commit()

    with pytest.raises(GenerationCompatibilityError, match="source snapshot is stale"):
        build_vector_generation(
            storage,
            conn,
            generation_id="gen-direct-stale",
            identity=identity,
            embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
            index_general=False,
            activate=True,
            expected_current=old["generation_id"],
        )
    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-direct-stale")["status"] == "failed"
    conn.close()


def test_shadow_build_failure_redacts_durable_and_returned_error(tmp_path):
    storage, conn, identity, old = _fixture(tmp_path)
    secret = "sk-" + "SHADOWBUILD123456789"
    private_path = "/home/a/private/shadow-input.json"

    class FailingEmbedder:
        def embed_texts(self, _texts):
            raise RuntimeError(f"provider failed api_key={secret} source={private_path}")

    with pytest.raises(RuntimeError) as captured:
        build_vector_generation(
            storage,
            conn,
            generation_id="gen-redacted-failure",
            identity=identity,
            embedder=FailingEmbedder(),
            index_general=False,
            expected_current=old["generation_id"],
        )
    manifest_error = str(generation_manifest(conn, "gen-redacted-failure")["error"])
    receipt_error = str(
        conn.execute(
            "SELECT error FROM vector_migration_receipts WHERE generation_id = ?",
            ("gen-redacted-failure",),
        ).fetchone()[0]
    )
    for text in (str(captured.value), manifest_error, receipt_error):
        assert secret not in text
        assert private_path not in text
        assert "[REDACTED" in text
    conn.close()


def test_half_build_failure_keeps_current_and_records_failed_receipt(tmp_path):
    storage, conn, identity, old = _fixture(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        build_vector_generation(
            storage,
            conn,
            generation_id="gen-failed",
            identity=identity,
            embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
            index_general=False,
            batch_size=1,
            expected_current=old["generation_id"],
            fail_after_rows=2,
        )

    assert current_generation(conn)["generation_id"] == old["generation_id"]
    assert generation_manifest(conn, "gen-failed")["status"] == "failed"
    receipt = conn.execute(
        "SELECT status, rows_built, error FROM vector_migration_receipts WHERE generation_id = ?",
        ("gen-failed",),
    ).fetchone()
    assert receipt["status"] == "failed"
    assert receipt["rows_built"] == 2
    assert "injected" in receipt["error"]
    conn.close()



def test_migration_cli_does_not_register_manifestless_legacy_companion(tmp_path):
    """Explicit migration builds a shadow without guessing legacy embedding identity."""

    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="truth-memory",
        scope_id="scope-a",
        platform="test",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="fixture",
        target="memory",
        content="manifestless legacy migration truth",
        allow_duplicate=True,
    )
    conn.commit()
    conn.close()

    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "enabled": True,
                    "backend": "sqlite-bruteforce",
                    "table_name": "memories",
                    "embedder": {
                        "provider": "local-hash",
                        "model": "hash-v1",
                        "dimensions": 16,
                    },
                },
                "retrieval": {"metric": "cosine"},
            }
        ),
        encoding="utf-8",
    )
    legacy = SQLiteBruteForceVectorStore(
        storage / "vector.sqlite3",
        table_name="memories",
        dimensions=16,
    )
    legacy.open()
    legacy.upsert_records(
        [
            {
                "id": "truth-memory",
                "scope_id": "scope-a",
                "source": "fixture",
                "target": "memory",
                "content": "manifestless legacy migration truth",
                "summary": "",
                "updated_at": "",
                "vector": [0.25] * 16,
            }
        ]
    )
    legacy.close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate.vector_generation.py"
    applied = subprocess.run(
        [
            sys.executable,
            str(script),
            "--hermes-home",
            str(home),
            "--generation-id",
            "gen-shadow-safe",
            "--apply",
            "--activate",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(applied.stdout)
    assert receipt["status"] == "activated"

    check = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    check.row_factory = sqlite3.Row
    generations = check.execute(
        "SELECT generation_id, status, metadata FROM vector_generations ORDER BY generation_id"
    ).fetchall()
    assert [(row["generation_id"], row["status"]) for row in generations] == [
        ("gen-shadow-safe", "active")
    ]
    assert all(
        json.loads(str(row["metadata"] or "{}")).get("provenance")
        != "legacy-config-inference"
        for row in generations
    )
    check.close()


def test_retire_existing_ready_cli_dry_run_then_apply(tmp_path):
    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=16,
        table_name="memories",
    )
    active = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    register_generation(
        conn,
        generation_id="gen-stale-ready",
        identity=identity,
        storage_path="vector-generations/gen-stale-ready",
        status="ready",
    )
    conn.commit()
    conn.close()
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate.vector_generation.py"
    common = [
        sys.executable,
        str(script),
        "--hermes-home",
        str(home),
        "--generation-id",
        "gen-stale-ready",
        "--expected-current",
        str(active["generation_id"]),
        "--retire-existing-ready",
        "--json",
    ]

    planned = subprocess.run([*common, "--dry-run"], check=True, capture_output=True, text=True)
    plan = json.loads(planned.stdout)
    assert plan["status"] == "planned"
    assert plan["writes"] == []
    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    assert (
        readonly.execute(
            "SELECT status FROM vector_generations WHERE generation_id = ?",
            ("gen-stale-ready",),
        ).fetchone()[0]
        == "ready"
    )
    readonly.close()

    applied = subprocess.run(
        [*common, "--apply", "--retirement-reason", "stale source cohort"],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(applied.stdout)
    assert receipt["status"] == "retired"
    assert receipt["physical_storage_retained"] is True
    check = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = check.execute(
        "SELECT status, metadata FROM vector_generations WHERE generation_id = ?",
        ("gen-stale-ready",),
    ).fetchone()
    assert row[0] == "retired"
    assert json.loads(row[1])["retirement"]["reason"] == "stale source cohort"
    check.close()
