from __future__ import annotations

import json
from pathlib import Path

import pytest

from p18_arm_provision import (
    BASELINE_578B,
    ArmProvisionError,
    provision_public_arm_plan,
)


ROOT = Path(__file__).resolve().parents[2]


def test_public_plan_has_eight_isolated_entries_without_core_or_host_side_effects(tmp_path: Path) -> None:
    receipt = provision_public_arm_plan(tmp_path / "plan", repo_root=ROOT)

    assert receipt["status"] == "PREPARATION_PASS"
    assert receipt["entry_count"] == 8
    assert receipt["network_calls"] == 0
    assert receipt["model_calls"] == 0
    assert receipt["host_started"] is False
    assert receipt["sealed_raw_or_gold_opened"] is False
    assert receipt["all_roots_distinct"] is True
    assert receipt["all_input_hashes_equal"] is True
    assert receipt["all_entries_without_core_schema"] is True
    assert all(entry["host"]["planned_storage"]["database_created"] is False for entry in receipt["entries"])
    assert all(entry["host"]["planned_storage"]["core_schema_created"] is False for entry in receipt["entries"])
    assert all(entry["host"]["planned_storage"]["claims_created"] is False for entry in receipt["entries"])
    assert all(Path(entry["input"]["path"]).is_file() for entry in receipt["entries"])


def test_baseline_578b_is_read_only_archived_and_codex_is_explicitly_unsupported(tmp_path: Path) -> None:
    receipt = provision_public_arm_plan(tmp_path / "plan", repo_root=ROOT)
    hermes = next(entry for entry in receipt["entries"] if entry["host"]["host_id"] == "hermes_a2a" and entry["arm_id"] == "B")
    codex = next(entry for entry in receipt["entries"] if entry["host"]["host_id"] == "codex_windows_desktop" and entry["arm_id"] == "B")

    snapshot = hermes["arm"]["code_source"]
    assert snapshot["status"] == "PASS"
    assert snapshot["baseline_ref"] == BASELINE_578B
    assert snapshot["project_name"] == "hermes-scope-recall"
    assert snapshot["project_version"] == "2.0.1"
    assert snapshot["install_entrypoint"] == "hermes-scope-recall = scope_recall.cli:main"
    assert snapshot["provider_entrypoint"] == "scope_recall.provider:ScopeRecallMemoryProvider"
    assert snapshot["installed"] is False
    assert snapshot["host_started"] is False
    assert Path(snapshot["archive_root"]).is_dir()
    assert codex["arm"]["status"] == "UNSUPPORTED"
    assert "codex_native" in codex["arm"]["reason"]


def test_archive_arm_is_independent_and_uses_only_projected_public_input(tmp_path: Path) -> None:
    receipt = provision_public_arm_plan(tmp_path / "plan", repo_root=ROOT)
    entries = [entry for entry in receipt["entries"] if entry["arm_id"] == "D"]

    assert len(entries) == 2
    assert {entry["storage"]["literal_query_matches"] for entry in entries} == {2}
    archive_hashes = {entry["storage"]["archive_sha256"] for entry in entries}
    assert len(archive_hashes) == 1
    for entry in entries:
        archive = Path(entry["storage"]["archive_path"])
        assert archive.is_file()
        rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
        assert all(set(row) == {"history", "query"} for row in rows)
        assert entry["storage"]["database_created"] is False
        assert entry["storage"]["core_schema_created"] is False
        assert entry["storage"]["claims_created"] is False


def test_candidate_and_native_arms_remain_explicitly_pending(tmp_path: Path) -> None:
    receipt = provision_public_arm_plan(tmp_path / "plan", repo_root=ROOT)
    for entry in receipt["entries"]:
        if entry["arm_id"] == "A":
            assert entry["arm"]["status"] == "PENDING_HOST_NATIVE_BINDING"
            assert entry["arm"]["code_source"]["sha256"] is None
        elif entry["arm_id"] == "C":
            assert entry["arm"]["status"] == "PENDING_CANDIDATE_FREEZE"
            assert entry["arm"]["code_source"]["sha256"] is None


def test_invalid_fixture_and_formal_root_fail_closed(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.jsonl"
    fixture.write_text('{"history": [], "query": {"text": "x"}, "gold": "forbidden"}\n', encoding="utf-8")
    with pytest.raises(ArmProvisionError, match="control/oracle"):
        provision_public_arm_plan(tmp_path / "bad-plan", repo_root=ROOT, fixture_path=fixture)
    with pytest.raises(ArmProvisionError, match="F:\\\\Agents"):
        provision_public_arm_plan(r"F:\Agents\P18-arm-plan", repo_root=ROOT)
