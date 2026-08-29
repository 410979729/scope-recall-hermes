"""Contract tests for the real installed N-1/N/N-1 release rehearsal."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

from scope_recall.scripts import rehearse_n_minus_one_window as stage_runner


ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = ROOT / "scripts" / "report.evidence_package.py"
SHA = "a" * 64


def _report_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_n_minus_one_window_evidence_test",
        REPORT_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(*, environment_id: str, python_sha: str, artifact_sha: str, version: str):
    return {
        "environment_id": environment_id,
        "python_executable_sha256": python_sha,
        "artifact_sha256": artifact_sha,
        "installed_distribution": f"hermes-scope-recall=={version}",
    }


def _stage(
    name: str,
    *,
    install: dict[str, str],
    version: str,
    before: str,
    after: str,
    details: dict[str, object],
):
    return {
        "stage": name,
        "python_environment_id": install["environment_id"],
        "installed_distribution": f"hermes-scope-recall=={version}",
        "artifact_sha256": install["artifact_sha256"],
        "python_executable_sha256": install["python_executable_sha256"],
        "command_sha256": SHA,
        "stdout_sha256": SHA,
        "stderr_sha256": SHA,
        "database_before_sha256": before,
        "database_after_sha256": after,
        "source_worktree_on_sys_path": False,
        "source_worktree_imported": False,
        "returncode": 0,
        "details": details,
    }


def _fixture():
    candidate = _install(
        environment_id="candidate-environment",
        python_sha="b" * 64,
        artifact_sha="c" * 64,
        version="2.0.0",
    )
    previous = _install(
        environment_id="n-minus-one-environment",
        python_sha="d" * 64,
        artifact_sha="e" * 64,
        version="1.10.3",
    )
    created = "1" * 64
    upgraded = "2" * 64
    lineage_id = hashlib.sha256(
        json.dumps(
            {
                "n_minus_one_created": created,
                "candidate_upgraded": upgraded,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stages = [
        _stage(
            "n_minus_one_create",
            install=previous,
            version="1.10.3",
            before="0" * 64,
            after=created,
            details={
                "memory_count": 2,
                "config_isolation_key_present": True,
                "vector_enabled": False,
            },
        ),
        _stage(
            "candidate_upgrade_write",
            install=candidate,
            version="2.0.0",
            before=created,
            after=upgraded,
            details={
                "n_minus_one_rows_preserved": 2,
                "claim_count": 1,
                "evidence_count": 1,
            },
        ),
        _stage(
            "n_minus_one_read_after_n",
            install=previous,
            version="1.10.3",
            before=upgraded,
            after=upgraded,
            details={"query_only": 1, "candidate_projection_readable": True},
        ),
        _stage(
            "candidate_final_verify",
            install=candidate,
            version="2.0.0",
            before=upgraded,
            after=upgraded,
            details={
                "claim_only_count": 0,
                "evidence_count": 1,
                "legacy_projection_count": 2,
            },
        ),
    ]
    receipt = {
        "schema_version": "scope-recall.n-minus-one-window.v1",
        "candidate_source_commit": "f" * 40,
        "candidate_source_tree": "9" * 40,
        "candidate_install_receipt_sha256": "3" * 64,
        "n_minus_one_install_receipt_sha256": "4" * 64,
        "candidate_n_minus_one_environment_mixed": False,
        "active_instance_touched": False,
        "neutral_runner_sha256": "5" * 64,
        "database_lineage_id": lineage_id,
        "result": "passed",
        "stages": stages,
    }
    return candidate, previous, receipt


def _validate(candidate, previous, receipt):
    _report_module()._validate_n_minus_one_window(
        receipt,
        source_commit="f" * 40,
        source_tree="9" * 40,
        candidate_install=candidate,
        candidate_install_sha256="3" * 64,
        n_minus_one_install=previous,
        n_minus_one_install_sha256="4" * 64,
        candidate_artifact_sha256=candidate["artifact_sha256"],
        neutral_runner_sha256="5" * 64,
    )


def test_n_minus_one_stage_uses_n_minus_one_python():
    candidate, previous, receipt = _fixture()
    receipt["stages"][0]["python_executable_sha256"] = candidate[
        "python_executable_sha256"
    ]
    with pytest.raises(Exception, match="stage identity"):
        _validate(candidate, previous, receipt)


def test_candidate_stage_uses_candidate_python():
    candidate, previous, receipt = _fixture()
    receipt["stages"][1]["python_executable_sha256"] = previous[
        "python_executable_sha256"
    ]
    with pytest.raises(Exception, match="stage identity"):
        _validate(candidate, previous, receipt)


def test_n_minus_one_environment_is_not_discarded():
    candidate, previous, receipt = _fixture()
    assert receipt["stages"][0]["python_environment_id"] == previous["environment_id"]
    assert receipt["stages"][2]["python_environment_id"] == previous["environment_id"]
    _validate(candidate, previous, receipt)


def test_n_minus_one_creates_real_legacy_truth():
    candidate, previous, receipt = _fixture()
    receipt["stages"][0]["details"]["memory_count"] = 1
    with pytest.raises(Exception, match="semantic proof"):
        _validate(candidate, previous, receipt)


def test_n_minus_one_fixture_is_valid_with_vector_explicitly_disabled(tmp_path):
    from scope_recall import installer

    hermes_home = tmp_path / "hermes-home"
    result = stage_runner._write_n_minus_one_truth(
        hermes_home / "scope-recall" / "memory.sqlite3",
        hermes_home,
    )
    config = json.loads(
        (hermes_home / "scope-recall" / "config.json").read_text(encoding="utf-8")
    )
    preflight = installer._upgrade_compatibility_preflight(
        hermes_home,
        installer.source_root(),
    )

    assert result["memory_count"] == 2
    assert result["vector_enabled"] is False
    assert config["vector"] == {"enabled": False}
    assert preflight["ok"] is True
    assert preflight["read_only"] is True


def test_candidate_migrates_real_n_minus_one_truth():
    candidate, previous, receipt = _fixture()
    receipt["stages"][1]["details"]["n_minus_one_rows_preserved"] = 1
    with pytest.raises(Exception, match="semantic proof"):
        _validate(candidate, previous, receipt)


def test_candidate_writes_claim_and_legacy_projection():
    candidate, previous, receipt = _fixture()
    receipt["stages"][1]["details"]["claim_count"] = 0
    with pytest.raises(Exception, match="semantic proof"):
        _validate(candidate, previous, receipt)


def test_n_minus_one_reads_n_written_legacy_projection():
    candidate, previous, receipt = _fixture()
    receipt["stages"][2]["details"]["candidate_projection_readable"] = False
    with pytest.raises(Exception, match="semantic proof"):
        _validate(candidate, previous, receipt)


def test_n_minus_one_does_not_mutate_additive_n_tables():
    candidate, previous, receipt = _fixture()
    receipt["stages"][2]["database_after_sha256"] = "8" * 64
    with pytest.raises(Exception, match="mutated"):
        _validate(candidate, previous, receipt)


def test_candidate_reopens_with_claim_projection_evidence_intact():
    candidate, previous, receipt = _fixture()
    receipt["stages"][3]["details"]["evidence_count"] = 0
    with pytest.raises(Exception, match="semantic proof"):
        _validate(candidate, previous, receipt)


def test_receipt_rejects_same_environment_id_for_n_and_n_minus_one():
    candidate, previous, receipt = _fixture()
    previous["environment_id"] = candidate["environment_id"]
    receipt["stages"][0]["python_environment_id"] = candidate["environment_id"]
    receipt["stages"][2]["python_environment_id"] = candidate["environment_id"]
    with pytest.raises(Exception, match="environments are equal"):
        _validate(candidate, previous, receipt)


def test_receipt_rejects_distribution_label_without_matching_interpreter_probe():
    candidate, previous, receipt = _fixture()
    receipt["stages"][0]["installed_distribution"] = "hermes-scope-recall==2.0.0"
    with pytest.raises(Exception, match="stage identity"):
        _validate(candidate, previous, receipt)


def test_receipt_rejects_neutral_runner_not_bound_to_candidate_source():
    candidate, previous, receipt = _fixture()
    receipt["neutral_runner_sha256"] = "7" * 64
    with pytest.raises(Exception, match="neutral runner source mismatch"):
        _validate(candidate, previous, receipt)


def test_receipt_rejects_broken_database_stage_lineage():
    candidate, previous, receipt = _fixture()
    receipt["stages"][2]["database_before_sha256"] = "7" * 64
    receipt["stages"][2]["database_after_sha256"] = "7" * 64
    with pytest.raises(Exception, match="stage lineage mismatch"):
        _validate(candidate, previous, receipt)


def test_receipt_rejects_forged_database_lineage_id():
    candidate, previous, receipt = _fixture()
    receipt["database_lineage_id"] = "7" * 64
    with pytest.raises(Exception, match="lineage ID mismatch"):
        _validate(candidate, previous, receipt)


def test_stage_runner_rejects_version_label_inconsistent_with_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        stage_runner,
        "_distribution_probe",
        lambda **_kwargs: pytest.fail("distribution probe must not run"),
    )
    with pytest.raises(stage_runner.RehearsalStageError, match="inconsistent"):
        stage_runner.run_stage(
            stage="n_minus_one_create",
            database=tmp_path / "truth.sqlite3",
            hermes_home=tmp_path / "home",
            source_root=ROOT,
            expected_version="2.0.0",
        )
