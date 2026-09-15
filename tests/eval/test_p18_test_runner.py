from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from p18_test_runner import (
    ARMS,
    HOSTS,
    MODEL_HISTORY_FIELDS,
    MODEL_QUERY_FIELDS,
    PRIMARY_PER_ARM_HOST,
    PRIMARY_PER_HOST,
    TOTAL_PRIMARY,
    _model_input_projection,
    _run_budget_rejection,
)


HERE = Path(__file__).resolve().parent
RUNNER = HERE / "p18_test_runner.py"
FIXTURE = HERE / "public_fixture.jsonl"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=HERE.parent.parent.parent,
        text=True,
        capture_output=True,
        check=False,
    )


def test_public_dry_run_is_zero_api_and_has_frozen_plan() -> None:
    result = _run("--dry-run-public", "--public-fixture", str(FIXTURE))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["network_calls"] == 0
    assert receipt["model_calls"] == 0
    assert receipt["execution_ready"] is False
    assert receipt["plan"]["primary_per_arm_host"] == PRIMARY_PER_ARM_HOST
    assert receipt["plan"]["primary_per_host"] == PRIMARY_PER_HOST
    assert receipt["plan"]["total_primary"] == TOTAL_PRIMARY
    assert len(receipt["arms"]) == 4
    assert len(receipt["hosts"]) == 2


def test_budget_rejection_happens_before_any_adapter() -> None:
    result = _run(
        "--dry-run-public",
        "--budget-rejection-test",
        "--public-fixture",
        str(FIXTURE),
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    rejection = receipt["budget_rejection"]
    assert rejection["status"] == "PASS"
    assert rejection["rejected_before_adapter"] is True
    assert rejection["network_calls"] == 0
    assert rejection["model_calls"] == 0


def test_real_core_public_chain_persists_reopens_recalls_and_renders() -> None:
    result = _run(
        "--core-public",
        "--public-fixture",
        str(FIXTURE),
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    core = receipt["core_public_run"]
    assert core["status"] == "PASS"
    assert core["network_calls"] == 0
    assert core["model_calls"] == 0
    assert core["data_directories_distinct"] is True
    assert core["cross_directory_read_isolation_negative"] is True
    assert len(core["instances"]) == 2
    for instance in core["instances"]:
        assert instance["persisted_sources"] == 1
        assert instance["initialized_sources"] == 0
        assert instance["persisted_ref"].startswith("event-")
        assert instance["packet_items"] >= 1
        assert instance["packet_sha256"]
        assert instance["memory_epoch"] is not None
        assert instance["render_present"] is True
        assert instance["render_sha256"]
        assert instance["model_input_fields"]["history"] == sorted(MODEL_HISTORY_FIELDS)
        assert instance["model_input_fields"]["query"] == sorted(MODEL_QUERY_FIELDS)


def test_real_budget_ledger_rejects_before_spy_transport() -> None:
    result = _run("--ledger-test", "--public-fixture", str(FIXTURE))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)["real_budget_ledger_test"]
    assert receipt["status"] == "PASS"
    assert receipt["error_type_budget_exhausted"] is True
    assert receipt["transport_calls"] == 0
    assert receipt["ledger_requests"] == 0


def test_offline_matrix_prepares_eight_isolated_backend_manifests() -> None:
    result = _run("--offline-matrix", "--public-fixture", str(FIXTURE))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    matrix = receipt["offline_backend_matrix"]
    assert matrix["manifest_count"] == 8
    assert matrix["all_data_directories_distinct"] is True
    assert matrix["all_host_execution_pending"] is True
    assert matrix["sequence_interface"]["queue_count_per_entry"] == PRIMARY_PER_ARM_HOST
    assert matrix["sequence_interface"]["model_input_only_history_query"] is True
    assert matrix["sequence_interface"]["gold_or_control_input"] is False
    entries = matrix["entries"]
    assert all(entry["queue_count"] == PRIMARY_PER_ARM_HOST for entry in entries)
    assert all(entry["queue_filled_slots"] == PRIMARY_PER_ARM_HOST for entry in entries)
    assert all(len(entry["queue_projection_sha256"]) == 64 for entry in entries)
    assert {(entry["host_id"], entry["arm_id"]) for entry in entries} == {
        (host, arm) for host in ("hermes_a2a", "codex_windows_desktop") for arm in ("A", "B", "C", "D")
    }
    assert all(Path(entry["manifest_path"]).is_file() for entry in entries)
    codex_baseline = next(entry for entry in entries if entry["host_id"] == "codex_windows_desktop" and entry["arm_id"] == "B")
    assert codex_baseline["backend_status"] == "UNSUPPORTED"
    hermes_baseline = next(entry for entry in entries if entry["host_id"] == "hermes_a2a" and entry["arm_id"] == "B")
    assert hermes_baseline["backend_status"] == "PASS"
    hermes_manifest = json.loads(Path(hermes_baseline["manifest_path"]).read_text(encoding="utf-8"))
    legacy_smoke = hermes_manifest["backend_evidence"]["legacy_api_smoke"]
    assert legacy_smoke["attempted"] is True
    assert legacy_smoke["inserted"] is True
    assert legacy_smoke["search_count"] >= 1
    archive_entry = next(entry for entry in entries if entry["arm_id"] == "D")
    archive_manifest = json.loads(Path(archive_entry["manifest_path"]).read_text(encoding="utf-8"))
    evidence = archive_manifest["backend_evidence"]
    assert evidence["semantic_admission"] is False
    assert evidence["core_imported"] is False
    assert evidence["claims_imported"] is False
    assert evidence["consolidation_imported"] is False
    assert evidence["literal_query_matches"] >= 1


def test_candidate_manifest_mismatch_refuses_execution(tmp_path: Path) -> None:
    source_root = tmp_path / "candidate"
    source_root.mkdir()
    (source_root / "marker.txt").write_text("TEST candidate", encoding="utf-8")
    manifest = tmp_path / "candidate-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "scope-recall.candidate-source-manifest.v1",
                "source_root": str(source_root),
                "source_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    result = _run(
        "--dry-run-public",
        "--public-fixture",
        str(FIXTURE),
        "--candidate-manifest",
        str(manifest),
        "--final-source-root",
        str(source_root),
    )
    assert result.returncode != 0
    assert "hash mismatch" in result.stderr


def test_model_input_projection_is_allowlist_constructed() -> None:
    row = {
        "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "TEST", "ignored": "drop"}],
        "query": {"text": "TEST", "oracle_like_extra": "drop"},
        "untrusted_extra": "drop",
    }
    projected = _model_input_projection(row)
    assert projected == {"history": [{"source_type": "human_direct", "speaker_role": "user", "text": "TEST"}], "query": {"text": "TEST"}}


def test_control_fields_are_rejected_without_network(tmp_path: Path) -> None:
    bad_fixture = tmp_path / "bad.jsonl"
    bad_fixture.write_text(
        '{"history":[],"query":{"text":"TEST"},"case_id":"forbidden"}\n',
        encoding="utf-8",
    )
    result = _run("--dry-run-public", "--public-fixture", str(bad_fixture))
    assert result.returncode != 0
    assert "control/oracle" in result.stderr


def test_arm_and_host_isolation_keys_are_unique() -> None:
    assert len({arm.isolation_key for arm in ARMS}) == len(ARMS)
    assert len({host.isolation_key for host in HOSTS}) == len(HOSTS)
    assert _run_budget_rejection()["planned_primary_calls"] == TOTAL_PRIMARY


@pytest.mark.parametrize("name", ["raw.jsonl", "gold.jsonl"])
def test_public_fixture_does_not_reference_sealed_artifacts(name: str) -> None:
    assert name not in FIXTURE.read_text(encoding="utf-8")
