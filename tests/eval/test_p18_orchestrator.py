from __future__ import annotations

import copy
from pathlib import Path

import pytest

from p18_history_loader import HistoryLoaderError, build_public_manifest
from p18_orchestrator import OrchestratorError, run_public_orchestration


def test_public_orchestration_creates_eight_isolated_pending_entries(tmp_path: Path) -> None:
    receipt = run_public_orchestration(tmp_path / "orchestrator")
    assert receipt["status"] == "PREPARATION_PASS"
    assert receipt["formal_execution_started"] is False
    assert receipt["network_calls"] == 0
    assert receipt["model_calls"] == 0
    assert receipt["gold_read"] is False
    assert receipt["entry_count"] == 8
    assert receipt["all_roots_distinct"] is True
    assert receipt["all_bindings_distinct"] is True

    entries = receipt["entries"]
    assert len({entry["source_manifest_sha256"] for entry in entries}) == 1
    assert len({ref for entry in entries if entry["arm_id"] == "C" for ref in entry["stages"]["L1_record"]["details"]["source_refs"]}) == 4
    for entry in entries:
        assert entry["binding"]["test_mode"] is True
        assert entry["formal_evidence"] is False
        if entry["arm_id"] == "C":
            assert entry["stages"]["L1_record"]["status"] == "PASS"
            assert entry["stages"]["L2_organization"]["status"] == "DIAGNOSTIC"
            assert entry["counts"]["claims"] == 0
        else:
            assert entry["stages"]["L1_record"]["status"] == "UNSUPPORTED"
            assert entry["stages"]["L2_organization"]["status"] == "UNSUPPORTED"
        assert entry["stages"]["L4_behavior"]["status"] == "NOT_RUN"
    assert all(entry["stages"]["L3_recall"]["status"] == "UNSUPPORTED" for entry in entries)


def test_diagnostic_worker_and_transport_are_marked_nonformal(tmp_path: Path) -> None:
    transport_calls: list[bytes] = []

    def transport(endpoint: str, body: bytes, timeout: float) -> tuple[int, bytes, dict[str, str]]:
        transport_calls.append(body)
        return 200, b'{"diagnostic":true}', {"Content-Type": "application/json"}

    receipt = run_public_orchestration(
        tmp_path / "diagnostic",
        diagnostic_worker=lambda core, context: {"worker_interface": "called"},
        diagnostic_hermes_transport=transport,
        hosts=("hermes_a2a",),
        arms=("C",),
    )
    entry = receipt["entries"][0]
    assert receipt["formal_execution_started"] is False
    assert receipt["network_calls"] is None
    assert entry["stages"]["L2_organization"]["status"] == "DIAGNOSTIC"
    assert entry["stages"]["L2_organization"]["reason"] == "injected_worker_is_not_formal_evidence"
    assert entry["stages"]["L3_recall"]["status"] == "DIAGNOSTIC"
    assert entry["stages"]["L3_recall"]["reason"] == "injected_test_transport_is_not_formal_evidence"
    assert len(transport_calls) == 1
    assert entry["stages"]["L4_behavior"]["status"] == "NOT_RUN"


def test_manifest_hash_is_verified_before_creating_run_root(tmp_path: Path) -> None:
    bad = copy.deepcopy(build_public_manifest())
    bad["manifest_sha256"] = "0" * 64
    root = tmp_path / "should-not-exist"
    with pytest.raises(HistoryLoaderError, match="manifest hash mismatch"):
        run_public_orchestration(root, bad)
    assert not root.exists()


def test_formal_agents_root_is_rejected() -> None:
    with pytest.raises(OrchestratorError, match="F:\\\\Agents"):
        run_public_orchestration(r"F:\Agents\TEST-P18-forbidden")
