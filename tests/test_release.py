"""Comprehensive release-gate tests for packaging, metadata, workflows, docs, and public contracts.

This file encodes what must stay true before a tag can be published."""

import importlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tomllib
import types
import zipfile
from pathlib import Path

import lancedb
import pyarrow as pa
import pytest
import yaml

from plugins.memory import load_memory_provider
from scope_recall.truth_connection import connect_truth_database

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import.openclaw.memory_lancedb_pro.py"
DOCTOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "doctor.py"
REPAIR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "repair.vector_index.py"
CHECK_RELEASE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check.release.py"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE_NAME] = package

embedders_module = importlib.import_module(f"{PACKAGE_NAME}.embedders")
build_embedder = embedders_module.build_embedder


def _write_local_debug_vector_config(hermes_home: Path) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
                    "fallback_embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class _FakeSentenceTransformer:
    def __init__(self, model: str, **kwargs):
        self.model = model
        self.kwargs = kwargs

    def get_embedding_dimension(self) -> int:
        return 384

    def encode(self, items, *, normalize_embeddings=True, convert_to_numpy=True):
        del normalize_embeddings, convert_to_numpy
        return [[1.0, *([0.0] * 383)] for _ in items]


def _install_fake_sentence_transformer(monkeypatch):
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.__spec__ = importlib.util.spec_from_loader(
        "sentence_transformers", loader=None
    )
    fake_module.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    monkeypatch.setattr(embedders_module, "SentenceTransformer", _FakeSentenceTransformer)
    embedders_module._SENTENCE_TRANSFORMER_CACHE.clear()
    for module in list(sys.modules.values()):
        module_file = str(getattr(module, "__file__", "") or "")
        if module_file and Path(module_file).name == "embedders.py":
            monkeypatch.setattr(
                module,
                "SentenceTransformer",
                _FakeSentenceTransformer,
                raising=False,
            )
            cache = getattr(module, "_SENTENCE_TRANSFORMER_CACHE", None)
            if isinstance(cache, dict):
                cache.clear()


def _package_version() -> str:
    import tomllib

    return tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _load_release_check_module(module_name: str = "scope_recall_check_release_contract"):
    spec = importlib.util.spec_from_file_location(module_name, CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)
    return release_check


def test_release_subprocess_runner_forces_utf8_without_mutating_caller_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_utf8_contract"
    )
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="中文输出", stderr="中文错误")

    monkeypatch.setattr(release_check.subprocess, "run", fake_run)
    caller_env = {"EXISTING": "preserved"}

    result = release_check.run(
        [sys.executable, "-c", "print('unused')"],
        env=caller_env,
    )

    assert result["stdout"] == "中文输出"
    assert result["stderr"] == "中文错误"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["text"] is True
    assert captured["env"] == {
        "EXISTING": "preserved",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    assert caller_env == {"EXISTING": "preserved"}


def _force_safe_native_vector_probe(doctor, monkeypatch) -> None:
    vector_module = importlib.import_module(doctor.vector_report.__module__)
    monkeypatch.setattr(
        vector_module,
        "native_vector_dependency_status",
        lambda: {"safe": True, "returncode": 0},
    )


def test_transcript_guard_is_required_in_source_wheel_sdist_and_pyright():
    release_check = _load_release_check_module(
        "scope_recall_check_release_transcript_guard"
    )
    version = _package_version()

    assert "transcript_overlap.py" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/transcript_overlap.py" in release_check.REQUIRED_WHEEL
    assert (
        f"hermes_scope_recall-{version}/transcript_overlap.py"
        in release_check.REQUIRED_SDIST
    )
    assert release_check.missing_sdist_members(
        set(release_check.REQUIRED_SDIST)
    ) == []
    assert release_check.missing_sdist_members(set()) == sorted(
        release_check.REQUIRED_SDIST
    )
    assert release_check.pyright_include_check()["ok"] is True


def test_release_gate_covers_every_package_and_script_python_source():
    release_check = _load_release_check_module(
        "scope_recall_check_release_complete_python_surface"
    )
    root_sources = {
        path.name for path in PLUGIN_ROOT.glob("*.py") if path.is_file()
    }
    script_sources = {
        path.relative_to(PLUGIN_ROOT).as_posix()
        for path in (PLUGIN_ROOT / "scripts").glob("*.py")
        if path.is_file()
    }
    internal_sources = {
        path.relative_to(PLUGIN_ROOT).as_posix()
        for path in (PLUGIN_ROOT / "_internal").rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    }
    expected_sources = root_sources | script_sources | internal_sources

    assert expected_sources <= release_check.REQUIRED_SOURCE_FILES
    assert {
        f"scope_recall/{source}" for source in expected_sources
    } <= release_check.REQUIRED_WHEEL

    pyright = release_check.pyright_include_check()
    assert set(pyright["required_source_py"]) == expected_sources
    assert pyright["ok"] is True, pyright["missing_pyright_include"]


def test_release_tree_scanner_does_not_exempt_force_added_sensitive_files(
    tmp_path,
    monkeypatch,
):
    release_check = _load_release_check_module(
        "scope_recall_check_release_force_added_sensitive_files"
    )
    secret = "github_" + "pat_" + ("A" * 40)
    (tmp_path / ".env").write_text("TOKEN=" + secret + "\n", encoding="utf-8")
    (tmp_path / "review-report.synthetic.md").write_text(
        "credential: " + secret + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    findings = release_check.scan_tree()["secrets"]

    assert any(item.startswith(".env:") for item in findings)
    assert any(item.startswith("review-report.synthetic.md:") for item in findings)
    assert secret not in "\n".join(findings)


def test_release_tree_scanner_skips_local_execution_evidence(
    tmp_path,
    monkeypatch,
):
    release_check = _load_release_check_module(
        "scope_recall_check_release_local_execution_evidence"
    )
    evidence = tmp_path / ".execution" / "LOCAL_ENVIRONMENT_MAP.json"
    evidence.parent.mkdir()
    private_path = "".join(("F", ":", "/Agents/private"))
    evidence.write_text(
        json.dumps({"private_path": private_path}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    assert release_check.scan_tree() == {
        "generated_artifacts": [],
        "secrets": [],
        "private_paths": [],
    }


def test_release_tree_scanner_skips_untracked_developer_environment(
    tmp_path,
    monkeypatch,
):
    release_check = _load_release_check_module(
        "scope_recall_check_release_developer_environment"
    )
    package = tmp_path / ".venv" / "Lib" / "site-packages" / "private_package"
    package.mkdir(parents=True)
    fake_token = "".join(("g", "h", "p", "_", "a" * 36))
    (package / "local.py").write_text(
        f"TOKEN = {fake_token!r}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    assert release_check.scan_tree() == {
        "generated_artifacts": [],
        "secrets": [],
        "private_paths": [],
    }


def test_packaged_tool_trace_skip_names_preserve_default_safety_set():
    config_module = importlib.import_module(f"{PACKAGE_NAME}.config")
    packaged = json.loads((PLUGIN_ROOT / "config.json").read_text(encoding="utf-8"))
    source_names = set(
        config_module.DEFAULT_CONFIG["journal"]["tool_trace_skip_names"]
    )
    packaged_names = set(packaged["journal"]["tool_trace_skip_names"])

    assert source_names <= packaged_names



def test_release_environment_check_reports_interpreter_and_required_modules(monkeypatch):
    release_check = _load_release_check_module("scope_recall_check_release_environment")

    monkeypatch.setattr(release_check.importlib.util, "find_spec", lambda name: object() if name != "lancedb" else None)

    report = release_check.release_environment_check()

    assert report["ok"] is False
    assert report["python_executable"] == sys.executable
    assert report["required_modules"]["pytest"] is True
    assert report["required_modules"]["lancedb"] is False
    assert report["missing_modules"] == ["lancedb"]
    assert report["install_command"] == "python -m pip install -e '.[dev,all]'"


def test_temporal_reflection_artifacts_are_release_packaged():
    release_check = _load_release_check_module("scope_recall_check_release_temporal_reflection")

    for source in [
        "fact_identity.py",
        "fact_actions.py",
        "fact_evidence.py",
        "evolution_policy.py",
        "fact_repository.py",
        "fact_executor.py",
        "fact_evolution.py",
        "fact_tooling.py",
        "temporal_facts.py",
        "temporal_query.py",
        "reflection.py",
        "reflection_llm.py",
        "reflection_grounding.py",
        "reflection_tooling.py",
        "doctor_temporal.py",
        "benchmarks/memory_evolution_cases.json",
        "benchmarks/reflection_cases.json",
        "scripts/benchmark.memory_evolution.py",
        "scripts/benchmark.reflection.py",
        "docs/fact-evolution-architecture.md",
        "docs/configuration.md",
    ]:
        assert source in release_check.REQUIRED_SOURCE_FILES

    for wheel_path in [
        "scope_recall/fact_identity.py",
        "scope_recall/fact_actions.py",
        "scope_recall/fact_evidence.py",
        "scope_recall/evolution_policy.py",
        "scope_recall/fact_repository.py",
        "scope_recall/fact_executor.py",
        "scope_recall/fact_evolution.py",
        "scope_recall/fact_tooling.py",
        "scope_recall/temporal_facts.py",
        "scope_recall/temporal_query.py",
        "scope_recall/reflection.py",
        "scope_recall/reflection_llm.py",
        "scope_recall/reflection_grounding.py",
        "scope_recall/reflection_tooling.py",
        "scope_recall/doctor_temporal.py",
        "scope_recall/benchmarks/memory_evolution_cases.json",
        "scope_recall/benchmarks/reflection_cases.json",
        "scope_recall/scripts/benchmark.memory_evolution.py",
        "scope_recall/scripts/benchmark.reflection.py",
        "scope_recall/docs/fact-evolution-architecture.md",
        "scope_recall/docs/configuration.md",
    ]:
        assert wheel_path in release_check.REQUIRED_WHEEL

    pyright = release_check.pyright_include_check()
    assert pyright["ok"] is True
    for module in [
        "fact_identity.py",
        "fact_actions.py",
        "fact_evidence.py",
        "evolution_policy.py",
        "fact_repository.py",
        "fact_executor.py",
        "fact_evolution.py",
        "fact_tooling.py",
        "temporal_facts.py",
        "temporal_query.py",
        "reflection.py",
        "reflection_llm.py",
        "reflection_grounding.py",
        "reflection_tooling.py",
        "doctor_temporal.py",
        "scripts/benchmark.memory_evolution.py",
        "scripts/benchmark.reflection.py",
    ]:
        assert module not in pyright["missing_pyright_include"]


def test_public_response_contract_files_are_release_packaged():
    release_check = _load_release_check_module("scope_recall_check_release_response_contracts")

    assert "response_schemas.py" in release_check.REQUIRED_SOURCE_FILES
    assert "docs/response-contracts.md" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/response_schemas.py" in release_check.REQUIRED_WHEEL
    assert "scope_recall/docs/response-contracts.md" in release_check.REQUIRED_WHEEL


def test_memory_governance_modules_are_release_packaged_and_type_checked():
    release_check = _load_release_check_module("scope_recall_check_release_memory_governance")
    modules = ["memory_admission.py", "memory_text_merge.py", "entity_quality.py", "tool_validation.py"]

    pyright = release_check.pyright_include_check()
    for module in modules:
        assert module in release_check.REQUIRED_SOURCE_FILES
        assert f"scope_recall/{module}" in release_check.REQUIRED_WHEEL
        assert module not in pyright["missing_pyright_include"]
    assert "docs/lifecycle.registry.md" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/docs/lifecycle.registry.md" in release_check.REQUIRED_WHEEL


def test_lifecycle_registry_contract_is_release_packaged_and_type_checked():
    release_check = _load_release_check_module(
        "scope_recall_check_release_lifecycle_registry"
    )
    modules = ["lifecycle_registry.py", "lifecycle_compat.py"]

    pyright = release_check.pyright_include_check()
    for module in modules:
        assert module in release_check.REQUIRED_SOURCE_FILES
        assert f"scope_recall/{module}" in release_check.REQUIRED_WHEEL
        assert module not in pyright["missing_pyright_include"]


def test_durable_work_contract_is_release_packaged_and_type_checked():
    release_check = _load_release_check_module(
        "scope_recall_check_release_durable_work"
    )

    pyright = release_check.pyright_include_check()
    assert "durable_work.py" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/durable_work.py" in release_check.REQUIRED_WHEEL
    assert "durable_work.py" not in pyright["missing_pyright_include"]
    assert "docs/durable-work.md" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/docs/durable-work.md" in release_check.REQUIRED_WHEEL


def test_sqlite_backup_module_is_release_packaged_and_type_checked():
    release_check = _load_release_check_module("scope_recall_check_release_sqlite_backup")

    pyright = release_check.pyright_include_check()
    assert "sqlite_backup.py" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/sqlite_backup.py" in release_check.REQUIRED_WHEEL
    assert "sqlite_backup.py" not in pyright["missing_pyright_include"]


def test_sqlite_recovery_module_is_release_packaged_and_type_checked():
    release_check = _load_release_check_module("scope_recall_check_release_sqlite_recovery")

    pyright = release_check.pyright_include_check()
    assert "sqlite_recovery.py" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/sqlite_recovery.py" in release_check.REQUIRED_WHEEL
    assert "sqlite_recovery.py" not in pyright["missing_pyright_include"]


def test_required_source_modules_are_pyright_covered():
    release_check = _load_release_check_module("scope_recall_check_release_pyright_coverage")

    result = release_check.pyright_include_check()

    assert result["ok"] is True
    for required in [
        "experience_evidence.py",
        "experience_quality.py",
        "experience_synthesis.py",
        "governance_scheduler.py",
        "task_boundary.py",
    ]:
        assert required in result["required_source_py"]
        assert required not in result["missing_pyright_include"]


def test_release_gate_progress_emits_machine_readable_stderr(capsys):
    release_check = _load_release_check_module("scope_recall_check_release_progress")

    release_check.progress("pytest:start")

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["event"] == "release_gate_progress"
    assert payload["stage"] == "pytest:start"
    assert "timestamp" in payload


def test_release_gate_pytest_command_disables_repository_cache():
    release_check = _load_release_check_module("scope_recall_check_release_pytest_command")

    command = release_check.release_pytest_command()

    assert command == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ]


def test_release_gate_streams_test_stages_without_captured_pipes(monkeypatch):
    release_check = _load_release_check_module("scope_recall_check_release_streaming")
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = None
        stderr = None

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(release_check.subprocess, "run", fake_run)

    assert release_check.release_stage_capture_output("release_invariants") is False
    assert release_check.release_stage_capture_output("pytest") is False
    assert release_check.release_stage_capture_output("ruff") is True
    result = release_check.run([sys.executable, "-m", "pytest"], capture_output=False)

    assert captured["capture_output"] is False
    assert result["stdout"] == ""
    assert result["stderr"] == ""


def test_release_invariant_manifest_is_versioned_unique_and_executable():
    release_check = _load_release_check_module("scope_recall_check_invariant_manifest")

    manifest = release_check.release_invariant_manifest()
    assert manifest["schema"] == "scope-recall.release-invariants.v1"
    assert manifest["suite_count"] == 13
    assert manifest["node_count"] >= 50
    suites = manifest["suites"]
    suite_ids = {suite["id"] for suite in suites}
    assert {
        "fact-authority-and-temporal-semantics",
        "journal-fact-closure-atomicity",
        "memory-mutation-serializability-and-atomicity",
        "activation-connection-capability-and-compensation",
        "installer-portability-and-n-minus-one-upgrade",
        "source-isolation-privacy",
        "relation-budget-and-schema-integrity",
        "independent-audit-2026-07-release-blockers",
        "seventh-independent-audit-2026-07-release-blockers",
        "sixteenth-independent-audit-performance-liveness-blockers",
        "seventeenth-independent-audit-security-transaction-liveness-blockers",
        "program0-g0-stabilization-contracts",
        "lexical-shadow-and-windows-rollout-release-blockers",
    } == suite_ids
    nodes = [node for suite in suites for node in suite["nodes"]]
    assert len(nodes) == len(set(nodes))
    assert all(node.startswith("tests/") and "::test_" in node for node in nodes)
    assert all((PLUGIN_ROOT / node.split("::", 1)[0]).is_file() for node in nodes)
    assert (
        "tests/test_sixth_audit_regressions.py::test_legacy_update_is_serializable_with_concurrent_fact_claim"
        in nodes
    )
    assert (
        "tests/test_source_isolation.py::test_historical_isolated_journal_backlog_is_not_consumed"
        in nodes
    )
    assert (
        "tests/test_seventh_independent_audit_regressions.py::test_complete_fts_second_start_is_bounded_and_write_free"
        in nodes
    )
    assert (
        "tests/test_program0_relation_containment.py::test_frequency_work_generation_never_reuses_after_change_row_is_consumed"
        in nodes
    )
    assert (
        "tests/test_program0_relation_containment.py::test_frequency_poison_terminally_blocks_dependent_focus_work"
        in nodes
    )
    assert (
        "tests/test_relation_cleanup_program0.py::test_cleanup_apply_is_backup_first_committed_and_idempotently_replayed"
        in nodes
    )
    assert (
        "tests/test_relation_cleanup_program0.py::test_cleanup_does_not_restore_ready_while_focus_work_remains"
        in nodes
    )
    assert (
        "tests/test_release.py::test_release_cleanup_preserves_repo_local_environments"
        in nodes
    )
    assert (
        "tests/test_lexical_cjk_retrieval.py::test_cjk_bigram_fallback_uses_indexed_postings_without_truth_scan"
        in nodes
    )
    assert (
        "tests/test_lexical_cjk_retrieval.py::test_shadow_rows_use_truth_rowid_identity"
        in nodes
    )
    assert (
        "tests/test_lexical_cjk_golden.py::test_held_out_chinese_golden_shadow_channel_metrics"
        in nodes
    )
    assert (
        "tests/test_lexical_benchmark.py::test_lexical_latency_contract_uses_target_derived_floor_on_fast_hosts"
        in nodes
    )
    assert (
        "tests/test_release.py::test_release_lexical_v2_accepts_target_bounded_fast_host_payload"
        in nodes
    )
    assert (
        "tests/test_lexical_migration_cli.py::test_backup_fence_blocks_raw_writer_after_backup_before_guards"
        in nodes
    )
    command = release_check.release_invariant_command()
    assert command[:6] == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    assert command[6:] == nodes
    assert "scripts/release.invariants.json" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/scripts/release.invariants.json" in release_check.REQUIRED_WHEEL


def test_windows_lane_remains_a_release_gate():
    workflow = yaml.safe_load((PLUGIN_ROOT / ".github/workflows/ci.yml").read_text())
    windows_job = workflow["jobs"]["windows-installer"]
    assert windows_job["runs-on"] == "windows-latest"
    assert windows_job["steps"][1]["with"]["python-version"] == "3.12"
    windows_command = windows_job["steps"][-1]["run"]
    assert "test_atomic_replace" in windows_command
    assert "test_confirmed_activation_compensation" in windows_command
    assert "test_maintenance_lease.py" in windows_command

    principal_stress = next(
        step
        for step in windows_job["steps"]
        if step.get("name") == "Stress Desktop principal convergence"
    )
    stress_command = str(principal_stress["run"])
    assert principal_stress["shell"] == "pwsh"
    assert principal_stress["env"]["PYTHONPATH"] == "${{ github.workspace }}"
    assert "1..20" in stress_command
    assert (
        "tests/test_desktop_principal.py::"
        "test_concurrent_desktop_principal_first_create_converges"
    ) in stress_command
    assert "if ($LASTEXITCODE -ne 0)" in stress_command

    matrix_job = workflow["jobs"]["test"]
    windows_full = [
        lane
        for lane in matrix_job["strategy"]["matrix"]["include"]
        if lane.get("name") == "windows-full-py312"
    ]
    assert matrix_job["runs-on"] == "${{ matrix.os }}"
    assert matrix_job.get("continue-on-error") is None
    assert len(windows_full) == 1
    assert windows_full[0]["os"] == "windows-latest"
    assert windows_full[0]["install-target"] == ".[lancedb,dev]"
    assert windows_full[0]["command"] == "python -m pytest -q"
    checkout_step = next(
        step
        for step in matrix_job["steps"]
        if step.get("name") == "Install Hermes runtime for plugin tests"
    )
    assert "rm -rf" not in str(checkout_step.get("run") or "")


def test_release_identity_requires_version_newer_than_latest_tag():
    release_check = _load_release_check_module("scope_recall_check_release_identity")

    current = _package_version()
    eligible = release_check.release_version_identity_check(tags=["v0.1.0"])
    reused = release_check.release_version_identity_check(tags=[f"v{current}"])
    namespaced_reused = release_check.release_version_identity_check(
        tags=[f"scope-recall-v{current}"]
    )
    namespaced_previous = release_check.release_version_identity_check(
        tags=["scope-recall-v0.1.0"]
    )
    waived = release_check.release_version_identity_check(
        tags=[f"v{current}"],
        development_snapshot=True,
    )
    tagged = release_check.release_version_identity_check(
        tags=[f"v{current}"],
        head_tags=[f"v{current}"],
        tagged_release=True,
    )
    namespaced_tagged = release_check.release_version_identity_check(
        tags=[f"scope-recall-v{current}"],
        head_tags=[f"scope-recall-v{current}"],
        tagged_release=True,
    )
    wrong_head = release_check.release_version_identity_check(
        tags=[f"v{current}"],
        head_tags=["v0.1.0"],
        tagged_release=True,
    )
    conflicting_modes = release_check.release_version_identity_check(
        tags=[f"v{current}"],
        head_tags=[f"v{current}"],
        tagged_release=True,
        development_snapshot=True,
    )

    assert eligible["ok"] is True
    assert eligible["release_eligible"] is True
    assert eligible["latest_release_tag"] == "v0.1.0"
    assert reused["ok"] is False
    assert reused["release_eligible"] is False
    assert reused["current_version"] == current
    assert reused["latest_release_tag"] == f"v{current}"
    assert namespaced_reused["ok"] is False
    assert namespaced_reused["release_eligible"] is False
    assert namespaced_reused["latest_release_tag"] == f"scope-recall-v{current}"
    assert namespaced_previous["ok"] is True
    assert namespaced_previous["release_eligible"] is True
    assert namespaced_previous["latest_release_tag"] == "scope-recall-v0.1.0"
    assert waived["ok"] is True
    assert waived["release_eligible"] is False
    assert waived["waived_for_development"] is True
    assert tagged["ok"] is True
    assert tagged["release_eligible"] is True
    assert tagged["tagged_head_match"] is True
    assert namespaced_tagged["ok"] is True
    assert namespaced_tagged["release_eligible"] is True
    assert namespaced_tagged["tagged_head_match"] is True
    assert namespaced_tagged["expected_release_tag"] == f"scope-recall-v{current}"
    assert wrong_head["ok"] is False
    assert wrong_head["release_eligible"] is False
    assert conflicting_modes["ok"] is False
    assert "mutually exclusive" in conflicting_modes["error"]


def test_v1106_release_candidate_identity_surfaces_are_consistent():
    """Bind the source candidate to every authoritative version surface."""

    expected_version = "1.10.6"
    release_check = _load_release_check_module("scope_recall_check_release_v1106_identity")
    lexical_generation = importlib.import_module(
        f"{PACKAGE_NAME}.lexical_generation"
    )
    relation_containment = importlib.import_module(
        f"{PACKAGE_NAME}.relation_containment"
    )
    plugin_manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    stability = (PLUGIN_ROOT / "docs" / "stability.md").read_text(encoding="utf-8")

    assert _package_version() == expected_version
    assert f"version: {expected_version}" in plugin_manifest
    assert release_check.PACKAGE_VERSION == expected_version
    assert release_check.RELEASE_READINESS_DOC == "docs/release-readiness.1.10.6.md"
    assert (PLUGIN_ROOT / release_check.RELEASE_READINESS_DOC).is_file()
    assert f"## [{expected_version}]" in changelog
    assert f"Version `{expected_version}`" in readme
    assert f"`scope-recall` {expected_version}" in stability
    assert lexical_generation.LEXICAL_SCHEMA_VERSION == 10812
    assert lexical_generation.LEXICAL_MIGRATION_PLUGIN_VERSION == "1.9.0"
    assert relation_containment.RELATION_CONTAINMENT_SCHEMA_VERSION == 10813
    assert relation_containment.RELATION_CONTAINMENT_MIGRATION_PLUGIN_VERSION == "1.10.6"

    identity = release_check.release_version_identity_check(
        tags=["v1.8.7", "v1.9.2", "v1.10.2", "v1.10.3"]
    )
    assert identity["ok"] is True
    assert identity["release_eligible"] is True
    assert identity["expected_release_tag"] == "v1.10.6"
    assert identity["latest_release_tag"] == "v1.10.3"


def test_v1106_changelog_starts_at_the_last_public_release():
    release_check = _load_release_check_module("scope_recall_check_release_v1106_changelog")

    assert release_check.PACKAGE_VERSION == "1.10.6"
    assert release_check.PUBLIC_RELEASE_BASELINE == "1.10.3"
    changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = release_check.changelog_section(changelog, "1.10.6")
    assert "since the last public release, `1.10.3`" in section
    for term in release_check.REQUIRED_CHANGELOG_TERMS:
        assert term in section
    historical = release_check.changelog_section(changelog, "1.10.2")
    assert "since the last public release, `1.9.2`" in historical
    historical_gate = release_check.changelog_completeness_check(
        changelog,
        version="1.10.2",
    )
    assert historical_gate["ok"] is True, historical_gate
    assert historical_gate["missing_terms"] == []


def test_v1106_public_wording_covers_program0_contracts():
    """Every current public surface must describe the Program 0 boundary."""

    release_check = _load_release_check_module(
        "scope_recall_check_release_v1106_program0"
    )
    changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    surfaces = {
        "changelog": release_check.changelog_section(changelog, "1.10.6"),
        "readme": (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8"),
        "stability": (PLUGIN_ROOT / "docs" / "stability.md").read_text(encoding="utf-8"),
        "readiness": (PLUGIN_ROOT / "docs" / "release-readiness.1.10.6.md").read_text(
            encoding="utf-8"
        ),
    }
    for name, text in surfaces.items():
        lowered = text.lower()
        assert "ci-required" in text, name
        assert "vector" in lowered, name
        assert "relation" in lowered, name
        assert "containment" in lowered, name
        assert "cleanup" in lowered, name


def test_v1106_public_surfaces_name_1103_as_the_public_baseline():
    release_check = _load_release_check_module("scope_recall_check_release_v1106_baseline_truth")

    truth = release_check.public_release_baseline_truth_check()
    assert truth["ok"] is True, truth
    assert release_check.PUBLIC_RELEASE_BASELINE == "1.10.3"
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    stability = (PLUGIN_ROOT / "docs" / "stability.md").read_text(encoding="utf-8")
    readiness = (PLUGIN_ROOT / "docs" / "release-readiness.1.10.6.md").read_text(encoding="utf-8")
    assert "last packaged `1.10.3`" in readme
    assert "last packaged `1.10.3`" in stability
    assert "Public release baseline: `1.10.3`." in readiness


def test_ruff_lint_contract_is_explicit_across_toolchain_upgrades():
    pyproject = tomllib.loads(
        (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    ruff = pyproject["tool"]["ruff"]

    assert ruff["lint"]["select"] == ["E4", "E7", "E9", "F"]
    assert ruff["extend-exclude"] == [".hermes-agent-src"]


def test_program1a_internal_packages_are_release_packaged() -> None:
    pyproject = tomllib.loads(
        (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    packages = set(pyproject["tool"]["setuptools"]["packages"])
    assert {
        "scope_recall._internal.application",
        "scope_recall._internal.compatibility",
    } <= packages

    release_check = _load_release_check_module(
        "scope_recall_check_release_program1a_packages"
    )
    assert {
        "scope_recall/_internal/application/memory_commands.py",
        "scope_recall/_internal/application/memory_queries.py",
        "scope_recall/_internal/application/runtime_state.py",
        "scope_recall/_internal/compatibility/registry.py",
    } <= release_check.REQUIRED_WHEEL


def test_productization_artifacts_are_release_gate_listed():
    release_check = _load_release_check_module("scope_recall_check_release_productization_artifacts")

    for source in [
        "candidate_extraction.py",
        "candidate_review.py",
        "candidate_store.py",
        "event_digest.py",
        "doctor_event_digest.py",
        "memory_browser.py",
        "skill_bridge.py",
        "external_bridge.py",
        "postgres_bridge.py",
        "pgvector_store.py",
        "experience_replay_generation.py",
        "docs/event-digest.md",
        "docs/governance-ui.md",
        "docs/install.md",
        "docs/skill-bridge.md",
        "docs/vector-backends.md",
        "examples/external_bridge/postgres_schema.sql",
        "benchmarks/golden_recall_hybrid_cases.json",
        "benchmarks/curated_recall_quality_cases_v2.json",
        "truth_connection.py",
        "sqlite_schema.py",
        "scripts/candidate.review.py",
        "scripts/memory.browser.py",
        "scripts/skill.bridge.py",
        "scripts/release_changelog.py",
        "docs/journal-source-restore.md",
        "journal_source_restore.py",
        "write_kernel.py",
        "_internal/runtime/composition.py",
        "_internal/recall/orchestrator.py",
    ]:
        assert source in release_check.REQUIRED_SOURCE_FILES

    for wheel_path in [
        "scope_recall/candidate_extraction.py",
        "scope_recall/candidate_review.py",
        "scope_recall/candidate_store.py",
        "scope_recall/event_digest.py",
        "scope_recall/doctor_event_digest.py",
        "scope_recall/memory_browser.py",
        "scope_recall/skill_bridge.py",
        "scope_recall/external_bridge.py",
        "scope_recall/postgres_bridge.py",
        "scope_recall/pgvector_store.py",
        "scope_recall/experience_replay_generation.py",
        "scope_recall/docs/event-digest.md",
        "scope_recall/docs/governance-ui.md",
        "scope_recall/docs/install.md",
        "scope_recall/docs/skill-bridge.md",
        "scope_recall/docs/vector-backends.md",
        "scope_recall/examples/external_bridge/postgres_schema.sql",
        "scope_recall/benchmarks/golden_recall_hybrid_cases.json",
        "scope_recall/benchmarks/curated_recall_quality_cases_v2.json",
        "scope_recall/truth_connection.py",
        "scope_recall/sqlite_schema.py",
        "scope_recall/scripts/candidate.review.py",
        "scope_recall/scripts/memory.browser.py",
        "scope_recall/scripts/skill.bridge.py",
        "scope_recall/scripts/release_changelog.py",
        "scope_recall/docs/journal-source-restore.md",
        "scope_recall/journal_source_restore.py",
        "scope_recall/write_kernel.py",
        "scope_recall/_internal/runtime/composition.py",
        "scope_recall/_internal/recall/orchestrator.py",
    ]:
        assert wheel_path in release_check.REQUIRED_WHEEL

    pyright = release_check.pyright_include_check()
    assert pyright["ok"] is True
    for module in ["event_digest.py", "doctor_event_digest.py", "experience_replay_generation.py", "skill_bridge.py"]:
        assert module not in pyright["missing_pyright_include"]


def test_release_readiness_note_is_release_packaged():
    release_check = _load_release_check_module("scope_recall_check_release_readiness_doc")

    expected_source = f"docs/release-readiness.{release_check.PACKAGE_VERSION}.md"
    expected_wheel = f"scope_recall/{expected_source}"
    assert release_check.RELEASE_READINESS_DOC == expected_source
    assert expected_source in release_check.REQUIRED_SOURCE_FILES
    assert expected_wheel in release_check.REQUIRED_WHEEL


def test_internal_plan_docs_are_not_release_contracts():
    release_check = _load_release_check_module("scope_recall_check_release_public_docs")

    assert "docs/upstream-recommendation.md" in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/docs/upstream-recommendation.md" in release_check.REQUIRED_WHEEL
    assert "docs/hermes-upstream-recommendation-plan.md" not in release_check.REQUIRED_SOURCE_FILES
    assert "scope_recall/docs/hermes-upstream-recommendation-plan.md" not in release_check.REQUIRED_WHEEL
    assert (PLUGIN_ROOT / "docs" / "plans").exists() is False
    assert release_check.public_doc_hygiene_check()["ok"] is True


def test_public_doc_hygiene_blocks_private_plan_markers(tmp_path, monkeypatch):
    release_check = _load_release_check_module("scope_recall_check_release_private_doc_markers")
    docs_plans = tmp_path / "docs" / "plans"
    docs_plans.mkdir(parents=True)
    private_user = "J" + "oy"
    private_agent = "玉" + "衡"
    (tmp_path / "README.md").write_text(
        f"The product promise {private_user} cares about\n", encoding="utf-8"
    )
    (docs_plans / "internal.md").write_text(
        f"由插件/{private_agent}自动提取，不需要 {private_user} 人工复审。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    result = release_check.public_doc_hygiene_check()

    assert result["ok"] is False
    assert result["forbidden_paths"] == ["docs/plans/internal.md"]
    markers = {(finding["path"], finding["marker"]) for finding in result["findings"]}
    assert ("README.md", "personal_name_joy") in markers
    assert ("README.md", "private_product_promise") in markers
    assert ("docs/plans/internal.md", "agent_persona_yuheng") in markers
    assert ("docs/plans/internal.md", "manual_review_private_context") in markers


def test_distribution_persona_hygiene_scans_runtime_and_packaged_benchmarks(tmp_path):
    release_check = _load_release_check_module("scope_recall_check_release_persona_hygiene")
    artifact = tmp_path / "candidate.whl"
    private_user = "J" + "oy"
    private_agent = "玉" + "衡"
    private_system = "Bei" + "dou"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "scope_recall/runtime.py",
            f"AUTHORITY = 'Ask {private_user} before release.'\n",
        )
        archive.writestr(
            "scope_recall/benchmarks/cases.json",
            json.dumps(
                {"candidate": f"{private_agent} uses {private_system}."},
                ensure_ascii=False,
            ),
        )
        archive.writestr(
            "scope_recall/tests/test_fixture.py",
            f"PRIVATE = '{private_user}'\n",
        )

    findings = release_check.scan_distribution_artifact(artifact)

    assert {item["marker"] for item in findings["private_persona"]} == {
        "agent_persona_yuheng",
        "private_project_beidou",
        "private_user_joy",
    }
    assert all("tests/test_fixture.py" not in item["path"] for item in findings["private_persona"])


def test_release_readiness_hygiene_excludes_deployment_local_state():
    release_check = _load_release_check_module("scope_recall_check_release_readiness_hygiene")
    current = (PLUGIN_ROOT / release_check.RELEASE_READINESS_DOC).read_text(encoding="utf-8")

    assert release_check.release_readiness_public_hygiene_check(current) == {"ok": True, "findings": []}
    assert release_check.release_readiness_tree_hygiene_check() == {"ok": True, "findings": []}

    private = """
Current read-only snapshot from /home/operator/.hermes:
- `severity=DEGRADED`
- `journal_unprocessed=7`
- `dead-letter:auth=1`
"""
    result = release_check.release_readiness_public_hygiene_check(private)

    assert result["ok"] is False
    assert {item["marker"] for item in result["findings"]} == {
        "embedded_live_snapshot",
        "embedded_severity_counter",
        "embedded_journal_counter",
        "embedded_dead_letter_counter",
        "embedded_private_path",
    }


def test_release_scan_rejects_forward_slash_windows_private_agent_root(
    tmp_path, monkeypatch
):
    release_check = _load_release_check_module(
        "scope_recall_check_release_forward_slash_agent_root"
    )
    drive = "E:"
    leaked = drive + "/" + "/".join(("Agents", "runtime", "windows", "hermes-yuheng"))
    (tmp_path / "notes.md").write_text("live root " + leaked + "\n", encoding="utf-8")
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    findings = release_check.scan_tree()

    assert findings["private_paths"] == ["notes.md:1"]
    joined = "\n".join(findings["private_paths"])
    assert leaked not in joined


def test_distribution_hygiene_blocks_plan_artifacts():
    release_check = _load_release_check_module("scope_recall_check_release_distribution_hygiene")

    names = {
        "scope_recall/docs/upstream-recommendation.md",
        "scope_recall/docs/plans/internal.md",
        "scope_recall/docs/hermes-upstream-recommendation-plan.md",
    }

    assert release_check.forbidden_distribution_entries(names) == [
        "scope_recall/docs/hermes-upstream-recommendation-plan.md",
        "scope_recall/docs/plans/internal.md",
    ]


def test_distribution_hygiene_blocks_private_runtime_modules():
    release_check = _load_release_check_module(
        "scope_recall_check_release_private_runtime_hygiene"
    )

    names = {
        "scope_recall/provider.py",
        "scope_recall/beidou_shared_memory.py",
        "scope_recall/shared_bridge_runtime.py",
        "scope_recall/shared_bridge_contract.py",
        "scope_recall/shared_bridge_outbox.py",
        "hermes_scope_recall-1.10.0/tests/phase0/oracles/contract_snapshot.json",
        "scope_recall/docs/internal.module-map.md",
        "hermes_scope_recall-1.10.0/tests/test_beidou_shared_bridge.py",
    }
    forbidden = set(release_check.forbidden_distribution_entries(names))
    assert "scope_recall/provider.py" not in forbidden
    assert "scope_recall/beidou_shared_memory.py" in forbidden
    assert "scope_recall/shared_bridge_runtime.py" in forbidden
    assert "scope_recall/shared_bridge_contract.py" in forbidden
    assert "scope_recall/shared_bridge_outbox.py" in forbidden
    assert "hermes_scope_recall-1.10.0/tests/phase0/oracles/contract_snapshot.json" in forbidden
    assert "scope_recall/docs/internal.module-map.md" in forbidden
    assert "hermes_scope_recall-1.10.0/tests/test_beidou_shared_bridge.py" in forbidden
    assert release_check.forbidden_public_source_present() == []


def test_changelog_completeness_gate_requires_current_release_terms():
    release_check = _load_release_check_module("scope_recall_check_release_changelog")

    version = release_check.PACKAGE_VERSION
    empty_current = f"# Changelog\n\n## [{version}] - 2026-07-15\n\n## [1.7.2] - 2026-07-12\n"
    failed = release_check.changelog_completeness_check(empty_current)
    assert failed["ok"] is False
    assert failed["section_found"] is False
    assert failed["missing_terms"] == list(release_check.REQUIRED_CHANGELOG_TERMS)
    assert set(failed["missing_terms"]) == {
        "ci-required",
        "Vector",
        "hashed constraints",
        "relation containment",
        "cap+1",
        "no partial",
        "poison",
        "operator cleanup",
        "health",
        "query zero-write",
    }

    missing_baseline = (
        f"# Changelog\n\n## [{version}] - 2026-08-03\n"
        + "\n".join(release_check.REQUIRED_CHANGELOG_TERMS)
    )
    baseline_failure = release_check.changelog_completeness_check(missing_baseline)
    assert baseline_failure["ok"] is False
    assert baseline_failure["baseline_found"] is False

    fenced_only = (
        "# Changelog\n\n```markdown\n"
        f"## [{version}] - 2099-01-01\n"
        f"This cumulative release covers changes since the last public release, "
        f"`{release_check.PUBLIC_RELEASE_BASELINE}`.\n"
        + "\n".join(release_check.REQUIRED_CHANGELOG_TERMS)
        + "\n```\n"
    )
    fenced_failure = release_check.changelog_completeness_check(fenced_only)
    assert fenced_failure["ok"] is False
    assert fenced_failure["section_found"] is False

    complete = (
        f"# Changelog\n\n## [{version}] - 2026-08-03\n"
        f"This cumulative release covers changes since the last public release, "
        f"`{release_check.PUBLIC_RELEASE_BASELINE}`.\n"
        + "\n".join(release_check.REQUIRED_CHANGELOG_TERMS)
    )
    passed = release_check.changelog_completeness_check(complete)
    assert passed["ok"] is True
    assert passed["baseline_found"] is True


def test_live_dashboard_waiver_check_detects_stale_snapshot():
    release_check = _load_release_check_module("scope_recall_check_release_live_waiver")
    dashboard = {
        "ok": True,
        "severity": "DEGRADED",
        "summary": {
            "journal_unprocessed": 724,
            "journal_dead_letter_replay_candidates": 272,
            "journal_llm_quarantine_runs": 4,
            "journal_digest_status": "degraded",
            "experience_duplicate_groups": 2,
            "experience_needs_review": 5,
            "memory_quality_active_hits": 0,
            "memory_secret_active": 0,
            "vector_status": "ready",
            "schema_migration_current": True,
        },
    }
    stale_readiness = """
Date: 2026-06-29

## Live dashboard waiver

Current read-only snapshot from the local Hermes home at audit time:

- `ok=true`
- `severity=DEGRADED`
- `journal_unprocessed=701`
- `journal_dead_letter_replay_candidates=272`
- `journal_llm_quarantine_runs=4`
- `journal_digest_status=degraded`
- `experience_duplicate_groups=2`
- `experience_needs_review=5`
- `memory_quality_active_hits=0`
- `memory_secret_active=0`
- `vector_status=ready`
- `schema_migration_current=true`

Reason:

Clearance condition: show `severity=OK` after live recovery.
"""

    snapshot = release_check.release_readiness_snapshot_values(stale_readiness)
    assert snapshot["severity"] == "DEGRADED"

    result = release_check.live_dashboard_waiver_check(dashboard, stale_readiness)

    assert result["ok"] is False
    assert result["live_ok"] is False
    assert result["waiver_used"] is True
    assert result["mismatches"] == [{"field": "journal_unprocessed", "recorded": "701", "current": "724"}]

    accepted = release_check.live_dashboard_waiver_check(dashboard, stale_readiness, accept_stale=True)
    assert accepted["ok"] is True
    assert accepted["accept_stale"] is True


def test_live_dashboard_file_check_is_disabled_without_explicit_payload():
    release_check = _load_release_check_module("scope_recall_check_release_live_disabled")

    assert release_check.live_dashboard_file_check("") == {"ok": True, "enabled": False}


def test_response_contract_doc_lists_public_schema_versions():
    from scope_recall.response_schemas import PUBLIC_RESPONSE_SCHEMA_VERSIONS

    doc = (PLUGIN_ROOT / "docs" / "response-contracts.md").read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/response-contracts.md" in readme
    assert "docs/response-contracts.md" in (PLUGIN_ROOT / "docs" / "contract.matrix.md").read_text(encoding="utf-8")
    for schema_version in PUBLIC_RESPONSE_SCHEMA_VERSIONS.values():
        assert schema_version in doc


def test_historical_release_readiness_is_marked_not_current():
    historical = (PLUGIN_ROOT / "docs" / "release-readiness.1.4.0.md").read_text(encoding="utf-8")
    upstream = (PLUGIN_ROOT / "docs" / "upstream-recommendation.md").read_text(encoding="utf-8")

    assert "Historical note" in historical
    assert "not the current release checklist" in historical
    assert "standalone-provider visibility" in upstream
    assert "after the `v1.4.0` release tag is created" not in upstream
    assert "Joy" not in upstream


def test_current_release_and_benchmark_docs_match_runtime_defaults():
    from scope_recall.config import DEFAULT_CONFIG

    current = _package_version()
    vector_only_floor = float(DEFAULT_CONFIG["retrieval"]["vector_only_min_score"])
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    benchmark = (PLUGIN_ROOT / "docs" / "benchmark.vector-threshold.md").read_text(
        encoding="utf-8"
    )

    assert f"Release `{current}`" in readme
    assert f"At the selected `{vector_only_floor:.2f}` default" in benchmark


def test_readme_public_version_matches_package_metadata():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    version = _package_version()

    assert f"Version `{version}`" in readme
    assert "Version `1.0.6`" not in readme


def test_default_config_includes_documented_retrieval_top_k():
    config_module = importlib.import_module(f"{PACKAGE_NAME}.config")
    source_config = json.loads((PLUGIN_ROOT / "config.json").read_text(encoding="utf-8"))

    assert source_config["retrieval"]["top_k"] == 5
    assert config_module.DEFAULT_CONFIG["retrieval"]["top_k"] == source_config["retrieval"]["top_k"]
    assert source_config["tool_schema_profile"] == "compact"
    assert config_module.DEFAULT_CONFIG["tool_schema_profile"] == "compact"
    assert source_config["tool_schema_extra_tools"] == []
    assert config_module.DEFAULT_CONFIG["tool_schema_extra_tools"] == []


def test_scope_recall_stats_reports_journal_digest_health(tmp_path, monkeypatch):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"journal": {"background_digest_enabled": False}}, ensure_ascii=False) + "\n", encoding="utf-8")
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-journal-health",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["journal_digest"]["last_status"] == "never_run"
        assert stats["journal_digest"]["consecutive_failures"] == 0

        module = sys.modules[plugin.__class__.__module__]

        def fail_digest(**_kwargs):
            return {"ok": False, "status": "error", "error": "simulated digest failure at /home/a/private"}

        monkeypatch.setattr(module, "run_journal_digest", fail_digest)
        plugin._run_background_journal_digest({"extractor": "heuristic", "max_entries_per_digest": 1})
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["journal_digest"]["last_status"] == "error"
        assert stats["journal_digest"]["consecutive_failures"] == 1
        assert "[REDACTED_PATH]" in stats["journal_digest"]["last_error"]
    finally:
        plugin.shutdown()



def test_readme_documents_hermes_venv_test_command():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "PYTHONPATH=/path/to/hermes-agent:" in readme
    assert "venv/bin/python -m pytest -q" in readme
    assert "Plain `pytest` from an unrelated Python environment" in readme


def test_readme_documents_artifact_anchors_and_secret_indexes():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Artifact anchors:" in readme
    assert "scope_recall_store_secret_index" in readme
    assert "secret_value_stored" in readme
    assert "plaintext secret" in readme
    assert "SQL/FTS/vector" in readme


def test_release_git_tree_check_ignores_ci_runtime_checkout(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_check_release", CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)

    def fake_run(cmd):
        stdout = (
            ""
            if cmd[:2] == ["git", "ls-files"]
            else "?? .hermes-agent-src/\n?? docs/new.md\n M README.md\n"
        )
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(release_check, "run", fake_run)

    result = release_check.git_tree_check(allow_dirty=False)

    assert result["ok"] is False
    assert "?? .hermes-agent-src/" not in result["untracked"]
    assert "?? docs/new.md" in result["untracked"]
    assert " M README.md" in result["dirty"]


def test_release_git_tree_check_allows_only_known_scratch(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_check_release_scratch", CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)

    def fake_run(cmd):
        stdout = (
            ""
            if cmd[:2] == ["git", "ls-files"]
            else "?? .hermes-agent-src/\n?? .execution/\n?? .hermes/\n?? build/\n"
        )
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(release_check, "run", fake_run)

    result = release_check.git_tree_check(allow_dirty=False)

    assert result == {
        "ok": True,
        "allow_dirty": False,
        "dirty": [],
        "untracked": [],
        "tracked_local_only": [],
    }


def test_release_git_tree_check_rejects_tracked_local_only_evidence(monkeypatch):
    release_check = _load_release_check_module(
        "scope_recall_check_release_tracked_execution_evidence"
    )

    def fake_run(cmd):
        stdout = (
            ".execution/LOCAL_ENVIRONMENT_MAP.json\n"
            ".venv/Lib/site-packages/private.py\n"
            if cmd[:2] == ["git", "ls-files"]
            else ""
        )
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(release_check, "run", fake_run)

    result = release_check.git_tree_check(allow_dirty=True)

    assert result["ok"] is False
    assert result["tracked_local_only"] == [
        ".execution/LOCAL_ENVIRONMENT_MAP.json",
        ".venv/Lib/site-packages/private.py",
    ]


def test_doctor_script_reports_source_versions():

    result = subprocess.run(
        [sys.executable, str(DOCTOR_PATH), "--source-root", str(PLUGIN_ROOT)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    version = _package_version()
    assert payload["ok"] is True
    assert payload["schema_version"] == "doctor_report.v1"
    assert payload["source"]["pyproject_version"] == version
    assert payload["source"]["plugin_version"] == version
    assert payload["source"]["readme_public_versions"] == [version]
    assert payload["checks"]["source_metadata"]["ok"] is True



def test_doctor_script_accepts_explicit_json_flag():
    result = subprocess.run(
        [sys.executable, str(DOCTOR_PATH), "--json", "--source-root", str(PLUGIN_ROOT)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True



def test_doctor_script_reports_missing_sqlite_truth_db(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(DOCTOR_PATH),
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["runtime"]["sqlite"]["status"] == "missing"
    assert "repair.vector_index.py" in "\n".join(payload["recommendations"])


def test_doctor_script_reports_missing_journal_schema(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = sqlite3.connect(storage / "memory.sqlite3")
    try:
        from scope_recall.sql_store import ensure_schema  # type: ignore[import-not-found]

        ensure_schema(conn)
    finally:
        conn.close()

    result = subprocess.run(
        [
            sys.executable,
            str(DOCTOR_PATH),
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["checks"]["journal_provenance"]["ok"] is False
    assert payload["runtime"]["journal"]["status"] == "schema_missing"



def test_doctor_vector_report_accepts_lancedb_list_tables_dict_response(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    _force_safe_native_vector_probe(doctor, monkeypatch)

    vector_dir = tmp_path / "scope-recall" / "lancedb"
    vector_dir.mkdir(parents=True)

    class FakeTable:
        def count_rows(self):
            return 7

    class FakeDB:
        def list_tables(self):
            return {"tables": ["memories"], "page_token": None}

        def open_table(self, name):
            assert name == "memories"
            return FakeTable()

    fake_lancedb = types.SimpleNamespace(connect=lambda path: FakeDB())
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)

    payload, check, recommendations = doctor.vector_report(tmp_path)

    assert payload["status"] == "ready"
    assert payload["tables"] == ["memories"]
    assert payload["row_count"] == 7
    assert check["ok"] is True
    assert recommendations == []



def test_doctor_vector_report_marks_search_smoke_failure_needs_repair(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    _force_safe_native_vector_probe(doctor, monkeypatch)

    vector_dir = tmp_path / "scope-recall" / "lancedb"
    vector_dir.mkdir(parents=True)

    class FakeVectorType:
        list_size = 3

    class FakeField:
        type = FakeVectorType()

    class FakeSchema:
        def field(self, name):
            assert name == "vector"
            return FakeField()

    class FakeTable:
        schema = FakeSchema()

        def count_rows(self):
            return 7

        def search(self, vector):
            raise RuntimeError("missing lance fragment")

    class FakeDB:
        def list_tables(self):
            return {"tables": ["memories"], "page_token": None}

        def open_table(self, name):
            assert name == "memories"
            return FakeTable()

    fake_lancedb = types.SimpleNamespace(connect=lambda path: FakeDB())
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)

    payload, check, recommendations = doctor.vector_report(tmp_path)

    assert payload["status"] == "needs_repair"
    assert payload["ready"] is False
    assert "missing lance fragment" in payload["error"]
    assert check["ok"] is False
    assert "repair.vector_index.py" in "\n".join(recommendations)



def test_doctor_vector_report_marks_dimension_mismatch_needs_repair(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    _force_safe_native_vector_probe(doctor, monkeypatch)

    vector_dir = tmp_path / "scope-recall" / "lancedb"
    vector_dir.mkdir(parents=True)

    class FakeVectorType:
        list_size = 256

    class FakeField:
        type = FakeVectorType()

    class FakeSchema:
        def field(self, name):
            assert name == "vector"
            return FakeField()

    class FakeQuery:
        def limit(self, value):
            assert value == 1
            return self

        def to_list(self):
            return []

    class FakeTable:
        schema = FakeSchema()

        def count_rows(self):
            return 7

        def search(self, vector):
            assert len(vector) == 256
            return FakeQuery()

    class FakeDB:
        def list_tables(self):
            return {"tables": ["memories"], "page_token": None}

        def open_table(self, name):
            assert name == "memories"
            return FakeTable()

    fake_lancedb = types.SimpleNamespace(connect=lambda path: FakeDB())
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)

    expected_embedder = {"source": "embedder", "provider": "openai-compatible", "model": "gemini-embedding-001", "dimensions": 3072}
    payload, check, recommendations = doctor.vector_report(tmp_path, expected_embedder=expected_embedder)

    assert payload["status"] == "needs_repair"
    assert payload["ready"] is False
    assert payload["dimensions"] == 256
    assert payload["expected_embedder"]["dimensions"] == 3072
    assert "dimension mismatch" in payload["error"]
    assert check["ok"] is False
    assert "repair.vector_index.py" in "\n".join(recommendations)


def test_doctor_expected_embedder_loads_profile_dotenv_before_fallback(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    monkeypatch.delenv("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY", raising=False)
    (tmp_path / ".env").write_text("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY=test-key\n", encoding="utf-8")
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "enabled": True,
                    "embedder": {
                        "provider": "openai-compatible",
                        "dimensions": 3072,
                        "model": "gemini-embedding-001",
                        "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
                    },
                    "fallback_embedder": {"provider": "local-hash", "dimensions": 256, "model": "hash-v1"},
                }
            }
        ),
        encoding="utf-8",
    )

    config = doctor.load_runtime_config(PLUGIN_ROOT, tmp_path)
    expected = doctor.expected_embedder_from_config(config)

    assert expected["source"] == "embedder"
    assert expected["dimensions"] == 3072
    assert "SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY" not in os.environ



def test_doctor_experience_config_summary_reports_auto_promotion_flag():
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    payload = doctor.experience_config_summary(
        {
            "experience": {
                "enabled": True,
                "prefetch_enabled": True,
                "auto_promotion_enabled": True,
                "auto_promotion_limit_sessions": 20,
                "promotion_require_verification": True,
            },
            "vector": {"embedder": {"api_key": "should-not-appear"}},
        }
    )

    assert payload == {
        "enabled": True,
        "prefetch_enabled": True,
        "auto_promotion_enabled": True,
        "auto_promotion_limit_sessions": 20,
        "promotion_require_verification": True,
    }
    assert "should-not-appear" not in str(payload)


def test_doctor_nightly_digest_report_surfaces_fallback_and_recent_errors(tmp_path):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    try:
        conn.execute(
            """
            CREATE TABLE nightly_digest_runs (
                id TEXT PRIMARY KEY,
                digest_date TEXT NOT NULL,
                source_db TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                extractor TEXT NOT NULL,
                model TEXT,
                dry_run INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        rows = [
            ("run-1", "2026-06-17", "memory.sqlite3", "2026-06-17T15:00:00+00:00", "2026-06-17T15:01:00+00:00", "llm", "model", 0, "error", 0, 0, 0, 0, "The read operation timed out", "{}"),
            ("run-2", "2026-06-18", "memory.sqlite3", "2026-06-18T15:00:00+00:00", "2026-06-18T15:01:00+00:00", "llm", "model", 0, "ok", 1, 0, 0, 0, None, "{}"),
            ("run-3", "2026-06-19", "memory.sqlite3", "2026-06-19T15:00:00+00:00", "2026-06-19T15:01:00+00:00", "llm", "model", 0, "ok_with_fallback", 2, 0, 0, 0, None, "{}"),
        ]
        conn.executemany(
            """
            INSERT INTO nightly_digest_runs (
                id, digest_date, source_db, started_at, finished_at, extractor, model, dry_run,
                status, inserted, updated, skipped, deleted, error, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor.nightly_digest_report(tmp_path)

    assert payload["status"] == "degraded"
    assert payload["latest_run"]["status"] == "ok_with_fallback"
    assert payload["runs"]["by_status"] == {"error": 1, "ok": 1, "ok_with_fallback": 1}
    assert payload["consecutive_errors"] == 0
    assert check == {"ok": True, "failures": []}
    joined = "\n".join(recommendations)
    assert "fallback" in joined
    assert "Recent nightly digest errors" in joined


def test_doctor_vector_report_accepts_sqlite_bruteforce_backend(tmp_path):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore  # type: ignore[import-not-found]

    store = SQLiteBruteForceVectorStore(tmp_path / "scope-recall" / "vector.sqlite3", table_name="memories", dimensions=2)
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "memory-1",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "non native vector backend",
                    "summary": "sqlite vector",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                }
            ]
        )
    finally:
        store.close()

    expected_embedder = {"source": "embedder", "provider": "local-debug", "model": "debug", "dimensions": 2}
    payload, check, recommendations = doctor.vector_report(tmp_path, expected_embedder=expected_embedder, backend="sqlite-bruteforce")

    assert payload["backend"] == "sqlite-bruteforce"
    assert payload["status"] == "ready"
    assert payload["row_count"] == 1
    assert payload["dimensions"] == 2
    assert payload["search_smoke"] == "ok"
    assert check["ok"] is True
    assert recommendations == []



def test_repair_vector_index_rebuilds_sqlite_bruteforce_backend(tmp_path):
    from scope_recall.journal import ensure_journal_schema  # type: ignore[import-not-found]
    from scope_recall.sql_store import ensure_schema, store_row  # type: ignore[import-not-found]

    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "enabled": True,
                    "backend": "sqlite-bruteforce",
                    "table_name": "memories",
                    "index_general": "false",
                    "embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
                },
                "retrieval": {"metric": "cosine"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    conn = connect_truth_database(storage_dir / "memory.sqlite3", mode="rwc")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        store_row(
            conn,
            memory_id="memory-1",
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
            content="SQLite brute force repair rebuilds from SQLite truth.",
        )
        store_row(
            conn,
            memory_id="general-1",
            scope_id="scope-a",
            platform="cli",
            user_id="joy",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="turn-user",
            target="general",
            content="General scratch rows stay out of vector repair when index_general is string false.",
        )
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, str(REPAIR_PATH), "--hermes-home", str(tmp_path), "--apply", "--no-backup"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["vector_backend"] == "sqlite-bruteforce"
    assert Path(payload["vector_path"]).parts[-2:] == ("scope-recall", "vector.sqlite3")
    assert payload["rows"] == 1
    assert payload["audit"] == {"physical_rows": 1, "unique_ids": 1, "duplicate_rows": 0, "duplicate_ids": 0}

    result = subprocess.run(
        [sys.executable, str(DOCTOR_PATH), "--source-root", str(PLUGIN_ROOT), "--hermes-home", str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    doctor_payload = json.loads(result.stdout)
    assert doctor_payload["runtime"]["vector_backend"] == "sqlite-bruteforce"
    assert doctor_payload["runtime"]["vector"]["status"] == "ready"



def test_doctor_expected_embedder_prefers_available_primary(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBEDDING_KEY", "present")
    config = {
        "vector": {
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "api_key_env": ["SCOPE_RECALL_TEST_EMBEDDING_KEY"],
            },
            "fallback_embedder": {"provider": "local-hash", "model": "hash-v1", "dimensions": 256},
        }
    }

    payload = doctor.expected_embedder_from_config(config)

    assert payload["source"] == "embedder"
    assert payload["provider"] == "openai-compatible"
    assert payload["model"] == "gemini-embedding-001"
    assert payload["dimensions"] == 3072



def test_doctor_reports_primary_unavailable_without_cross_space_fallback(monkeypatch):
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    monkeypatch.delenv("SCOPE_RECALL_TEST_EMBEDDING_KEY", raising=False)
    config = {
        "vector": {
            "embedder": {
                "provider": "openai-compatible",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
                "api_key_env": ["SCOPE_RECALL_TEST_EMBEDDING_KEY"],
            },
            "fallback_embedder": {"provider": "local-hash", "model": "hash-v1", "dimensions": 256},
        }
    }

    payload = doctor.expected_embedder_from_config(config)

    assert payload["source"] == "embedder"
    assert payload["provider"] == "openai-compatible"
    assert payload["model"] == "gemini-embedding-001"
    assert payload["dimensions"] == 3072
    assert payload["available"] is False
    assert payload["fallback_available"] is True
    assert payload["fallback_compatible"] is False



def test_plugin_manifest_contract_matches_provider_lifecycle_hooks():
    release_check = _load_release_check_module("scope_recall_check_release_manifest_contract")

    manifest_hooks = set(release_check.parse_plugin_manifest_hooks((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")))
    provider_hooks = set(release_check.provider_lifecycle_hook_methods())

    assert manifest_hooks == provider_hooks
    assert {"on_pre_compress", "on_session_switch"} <= manifest_hooks



def test_release_gate_stable_tool_names_cover_schema_surfaces_and_dispatch():
    release_check = _load_release_check_module("scope_recall_check_release_tool_contract")
    surfaces = release_check.provider_tool_schema_names_by_surface()

    for surface in ("compact", "standard", "experience", "maintenance", "all_referenced"):
        assert set(surfaces[surface]) <= release_check.STABLE_TOOL_NAMES
    assert {"scope_recall_memory", "scope_recall_entity"} <= set(surfaces["compact"])
    assert release_check.STABLE_TOOL_NAMES <= set(release_check.tool_dispatcher_names())



def test_release_gate_product_contract_is_clean():
    release_check = _load_release_check_module("scope_recall_check_release_product_contract")

    product_contract = release_check.product_contract_check()

    assert product_contract["ok"] is True, product_contract["failures"]


def test_release_gate_proves_retired_relation_rebuild_has_no_worker_body():
    release_check = _load_release_check_module(
        "scope_recall_check_release_retired_relation_rebuild"
    )

    result = release_check.retired_relation_rebuild_source_gate()

    assert result == {"ok": True, "findings": []}


def test_retired_relation_rebuild_gate_rejects_hidden_sql_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_retired_relation_rebuild_adversarial"
    )
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    (tmp_path / "relation_rebuild_queue.py").write_text(
        """
def enqueue_relation_rebuild(conn, **kwargs):
    conn.execute('INSERT INTO relation_rebuild_queue DEFAULT VALUES')

def resolve_relation_rebuild(conn, **kwargs):
    return 0

def claim_relation_rebuild_events(conn, **kwargs):
    return []

def drain_relation_rebuild_queue(conn, **kwargs):
    return {'disabled': True}

def seed_scope_relation_rebuilds(conn, **kwargs):
    return {'disabled': True}
""",
        encoding="utf-8",
    )

    result = release_check.retired_relation_rebuild_source_gate()

    assert result["ok"] is False
    assert any(
        finding["code"] == "retired_surface_can_mutate_sqlite"
        for finding in result["findings"]
    )



def test_pypi_workflow_runs_release_gate_before_publish():
    pypi_workflow = (PLUGIN_ROOT / ".github" / "workflows" / "pypi.yml").read_text(encoding="utf-8")
    release_workflow = (PLUGIN_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    ci_workflow = (PLUGIN_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "fetch-depth: 0" in ci_workflow
    assert "scripts/check.release.py --tagged-release" in pypi_workflow
    assert "fetch-depth: 0" in pypi_workflow
    assert "python -m pip install --require-hashes -r constraints/release-hashed.txt" in pypi_workflow
    assert 'python -m pip install --no-deps ".[lancedb,dev]"' in pypi_workflow
    assert pypi_workflow.index("scripts/check.release.py") < pypi_workflow.index("pypa/gh-action-pypi-publish")
    assert "  release:" not in pypi_workflow
    assert "types: [published]" not in pypi_workflow
    assert "repository_dispatch:" in pypi_workflow
    assert "scope-recall-pypi-publish" in pypi_workflow
    assert "workflow_dispatch:" in pypi_workflow
    assert "release_tag:" in pypi_workflow
    assert "skip-existing" not in pypi_workflow
    assert "PyPI publish" in pypi_workflow
    assert "Invalid release tag" in pypi_workflow
    assert "Verify tag matches package version" in pypi_workflow
    assert "echo \"ref=${{ inputs.tag }}\"" not in pypi_workflow

    assert "scripts/check.release.py --tagged-release" in release_workflow
    assert "fetch-depth: 0" in release_workflow
    assert "pypa/gh-action-pypi-publish" not in release_workflow
    assert "id-token: write" not in release_workflow
    assert "Upload release distributions" in release_workflow
    assert "Trigger PyPI publish workflow" in release_workflow
    assert "scope-recall-pypi-publish" in release_workflow
    assert "repos/${GITHUB_REPOSITORY}/dispatches" in release_workflow
    assert "continue-on-error: true" not in release_workflow
    assert "Invalid release tag" in release_workflow
    assert "Verify tag matches package version" in release_workflow
    assert "echo \"tag=${{ github.event.inputs.tag }}\"" not in release_workflow




def test_release_gate_requires_doctor_script():
    release_script = (PLUGIN_ROOT / "scripts" / "check.release.py").read_text(encoding="utf-8")

    assert '"scripts/doctor.py"' in release_script



def test_release_gate_runs_ruff_and_pyright_checks():
    release_script = (PLUGIN_ROOT / "scripts" / "check.release.py").read_text(encoding="utf-8")

    assert '[sys.executable, "-m", "ruff", "check", "."]' in release_script
    assert '[sys.executable, "-m", "pyright"]' in release_script


def test_release_compileall_excludes_local_private_environments(
    monkeypatch,
    tmp_path,
):
    release_check = _load_release_check_module(
        "scope_recall_check_release_compileall_boundary"
    )
    (tmp_path / "root.py").write_text("ROOT = True\n", encoding="utf-8")
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("MODULE = True\n", encoding="utf-8")
    for directory in release_check.RELEASE_TRAVERSAL_EXCLUDED_DIRS:
        protected = tmp_path / directory
        protected.mkdir(parents=True, exist_ok=True)
        (protected / "private.py").write_text("PRIVATE = True\n", encoding="utf-8")
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    command = release_check.release_compileall_command()

    assert command[:4] == [sys.executable, "-m", "compileall", "-q"]
    assert command[4:] == ["package/module.py", "root.py"]


def test_release_cleanup_preserves_repo_local_environments(monkeypatch, tmp_path):
    release_check = _load_release_check_module("scope_recall_check_release_cleanup")
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    protected_sentinels = []
    for directory in (
        ".venv",
        "venv",
        ".hermes-agent-src",
        ".execution",
        ".hermes",
        ".git",
    ):
        sentinel = (
            tmp_path
            / directory
            / "Lib"
            / "site-packages"
            / "build"
            / "__init__.py"
        )
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("keep protected local state\n", encoding="utf-8")
        protected_sentinels.append(sentinel)
    pycache = tmp_path / "pkg" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "mod.pyc").write_bytes(b"pyc")
    generated_build = tmp_path / "pkg" / "build"
    generated_build.mkdir()
    (generated_build / "generated.txt").write_text("remove\n", encoding="utf-8")

    release_check.cleanup_generated()

    assert all(sentinel.is_file() for sentinel in protected_sentinels)
    assert not pycache.exists()
    assert not generated_build.exists()


def test_release_cleanup_removes_current_sdist_staging_root(monkeypatch, tmp_path):
    release_check = _load_release_check_module("scope_recall_check_release_sdist_cleanup")
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    sdist_staging = tmp_path / f"hermes_scope_recall-{release_check.PACKAGE_VERSION}"
    sdist_staging.mkdir()
    (sdist_staging / "memory_quality.py").write_text("generated\n", encoding="utf-8")
    unrelated = tmp_path / "hermes_scope_recall-not-a-version"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")

    release_check.cleanup_generated()

    assert not sdist_staging.exists()
    assert (unrelated / "keep.txt").is_file()


def test_ci_installs_release_gate_lint_dependency():
    import tomllib

    pyproject = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
    workflow = (PLUGIN_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert any(
        dep == "ruff" or dep.startswith(("ruff<", "ruff>", "ruff=", "ruff!", "ruff~", "ruff["))
        for dep in dev_deps
    )
    assert '".[lancedb,dev]"' in workflow



def test_default_embedder_targets_gemini_openai_compatible_api():
    embedder = build_embedder(
        {
            "provider": "openai-compatible",
            "model": "gemini-embedding-001",
            "dimensions": 3072,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
        }
    )
    info = embedder.describe()
    assert info["provider"] == "openai-compatible"
    assert info["model"] == "gemini-embedding-001"
    assert info["dimensions"] == 3072
    assert info["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"



def test_sentence_transformers_embedder_builds_local_interface_without_loading_weights():
    sentence_transformers_available = bool(importlib.util.find_spec("sentence_transformers"))
    embedder = build_embedder(
        {
            "provider": "sentence-transformers",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        }
    )
    info = embedder.describe()
    assert info["provider"] == "sentence-transformers"
    assert info["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert info["dimensions"] >= 384
    assert embedder.is_available() is sentence_transformers_available


def test_sentence_transformers_embedder_can_encode_locally_when_requested(monkeypatch):
    _install_fake_sentence_transformer(monkeypatch)
    embedder = build_embedder(
        {
            "provider": "sentence-transformers",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        }
    )
    vectors = embedder.embed_texts(["scope recall local embedder smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 384


@pytest.mark.skipif(
    os.getenv("SCOPE_RECALL_RUN_SENTENCE_TRANSFORMERS_INTEGRATION") != "1"
    or not bool(importlib.util.find_spec("sentence_transformers")),
    reason="real sentence-transformers integration is opt-in because it may download/load HF model weights",
)
def test_sentence_transformers_embedder_real_model_integration():
    embedder = build_embedder(
        {
            "provider": "sentence-transformers",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "device": "cpu",
        }
    )
    vectors = embedder.embed_texts(["scope recall local embedder smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) >= 384


def test_sentence_transformers_provider_path_uses_local_vector_dimensions(tmp_path, monkeypatch):
    _install_fake_sentence_transformer(monkeypatch)
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "backend": "sqlite-bruteforce",
                    "embedder": {
                        "provider": "sentence-transformers",
                        "model": "sentence-transformers/all-MiniLM-L6-v2",
                    },
                    "fallback_embedder": {
                        "provider": "local-hash",
                        "dimensions": 256,
                        "model": "hash-v1",
                    },
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-local-model",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Local sentence-transformers provider smoke test.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["vector"]["ready"] is True
        assert stats["vector"]["embedder"]["provider"] == "sentence-transformers"
        assert stats["vector"]["embedder"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"
        assert stats["vector"]["embedder"]["dimensions"] == 384
        assert stats["vector"]["row_count"] == 1
    finally:
        plugin.shutdown()



def test_ordinary_vector_startup_defers_stale_sweep_to_explicit_repair(tmp_path):
    _write_local_debug_vector_config(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Deploy services with uv run app.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        plugin._vector_store.upsert_records(
            [
                {
                    "id": "stale-row",
                    "scope_id": plugin._scope_id,
                    "source": "test",
                    "target": "memory",
                    "content": "obsolete row",
                    "summary": "obsolete row",
                    "updated_at": "1970-01-01T00:00:00+00:00",
                    "vector": [0.0] * plugin._embedder.dimensions,
                }
            ]
        )
        assert plugin._vector_store.count_rows() == 2
    finally:
        plugin.shutdown()

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        from scope_recall.vector_runtime import sync_vector_index

        assert plugin._vector_store is not None
        assert plugin._vector_store.count_rows() == 2
        assert "stale-row" in plugin._vector_store.list_ids()
        assert sync_vector_index(plugin) == 1
        assert plugin._vector_store.count_rows() == 1
        assert "stale-row" not in plugin._vector_store.list_ids()
    finally:
        plugin.shutdown()



def test_ordinary_vector_startup_defers_duplicate_detection_to_explicit_audit(tmp_path):
    _write_local_debug_vector_config(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Duplicate vector rows should be repaired by id.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        memory_id = payload["id"]
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        assert plugin._embedder is not None
        require_table = getattr(plugin._vector_store, "_require_table", None)
        if not callable(require_table):
            pytest.skip("physical duplicate-row audit requires the LanceDB backend")
        require_table().add(
            [
                {
                    "id": memory_id,
                    "scope_id": plugin._scope_id,
                    "source": "test-duplicate",
                    "target": "memory",
                    "content": "obsolete duplicate row",
                    "summary": "obsolete duplicate row",
                    "updated_at": "1970-01-01T00:00:00+00:00",
                    "vector": [0.0] * plugin._embedder.dimensions,
                }
            ]
        )
        assert plugin._vector_store.count_rows() == 2
        assert plugin._vector_store.audit_counts()["duplicate_rows"] == 1
    finally:
        plugin.shutdown()

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        from scope_recall.vector_runtime import refresh_vector_audit, sync_vector_index

        assert plugin._vector_store is not None
        assert plugin._vector_store.count_rows() == 2
        assert refresh_vector_audit(plugin)["duplicate_rows"] == 1
        with pytest.raises(RuntimeError, match="duplicate row"):
            sync_vector_index(plugin)
        assert plugin._vector_store.count_rows() == 2
    finally:
        plugin.shutdown()



def test_vector_upsert_failure_stays_retryable_without_losing_sqlite_row(tmp_path, monkeypatch):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    _write_local_debug_vector_config(tmp_path)
    plugin.initialize(
        "session-vector-failure",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._vector_store is not None

        def fail_upsert(rows):
            raise RuntimeError("simulated LanceDB delete failure")

        monkeypatch.setattr(plugin._vector_store, "upsert_records", fail_upsert)
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "SQLite truth survives vector upsert failure.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        assert plugin._conn is not None
        count = plugin._conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", (payload["id"],)).fetchone()[0]
        assert count == 1
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["vector"]["ready"] is True
        assert stats["vector"]["status"] == "degraded"
        assert "simulated LanceDB delete failure" in stats["vector"]["message"]
        event = plugin._conn.execute(
            "SELECT status, operation, generation_id FROM vector_outbox WHERE memory_id = ?",
            (payload["id"],),
        ).fetchone()
        assert event is not None
        assert event["status"] == "retry"
        assert event["operation"] == "upsert"
        assert event["generation_id"] == plugin._vector_generation_id
        failed_memory_id = payload["id"]
    finally:
        plugin.shutdown()

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-vector-replay",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._conn is not None
        replayed = plugin._conn.execute(
            "SELECT status, completed_at FROM vector_outbox WHERE memory_id = ?",
            (failed_memory_id,),
        ).fetchone()
        assert replayed is not None
        assert replayed["status"] == "completed"
        assert replayed["completed_at"]
        assert plugin._vector_store is not None
        assert failed_memory_id in plugin._vector_store.list_ids()
    finally:
        plugin.shutdown()



def test_default_runtime_bootstraps_fallback_only_when_no_generation_exists(tmp_path, monkeypatch):
    for name in ("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-fallback",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        assert plugin._embedder is not None
        assert plugin._embedder.provider == "local-hash"
        assert plugin._vector_status == "ready"
        assert "initialized fresh generation with fallback" in plugin._vector_message
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["vector"]["status"] == "ready"
        assert "initialized fresh generation with fallback" in stats["vector"]["message"]
        manifest = plugin._conn.execute(
            "SELECT * FROM vector_generations WHERE generation_id = ?",
            (plugin._vector_generation_id,),
        ).fetchone()
        assert manifest is not None
        assert manifest["status"] == "active"
        assert manifest["provider"] == "local-hash"
        assert manifest["model"] == "hash-v1"
        assert manifest["dimensions"] == 256
    finally:
        plugin.shutdown()



def test_vector_store_preserves_active_generation_when_embedder_dimensions_change(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "backend": "sqlite-bruteforce",
                    "embedder": {
                        "provider": "local-hash",
                        "dimensions": 3072,
                        "model": "hash-v1",
                    },
                    "fallback_embedder": {
                        "provider": "local-hash",
                        "dimensions": 256,
                        "model": "hash-v1",
                    },
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin.flush(timeout=5.0)
        assert plugin._vector_store is not None
        assert plugin._vector_store.dimensions == 3072
        manifest = plugin._require_conn().execute(
            "SELECT backend, dimensions FROM vector_generations "
            "WHERE status = 'active'"
        ).fetchone()
        assert tuple(manifest) == ("sqlite-bruteforce", 3072)
    finally:
        plugin.shutdown()

    config_path.write_text(
        json.dumps(
            {
                "vector": {
                    "embedder": {
                        "provider": "local-hash",
                        "dimensions": 256,
                        "model": "hash-v1",
                    }
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        assert plugin._vector_store is None
        assert plugin._vector_status == "needs_repair"
        status = plugin.vector_status_view()
        assert status["state"] == "needs_repair"
        assert status["reason_code"] == "identity_mismatch"
        assert status["auto_recoverable"] is False
        assert status["repair_required"] is True
        assert status["usable_for_query"] is False
        assert "identity mismatch" in plugin._vector_message
        manifest = plugin._require_conn().execute(
            "SELECT backend, dimensions FROM vector_generations "
            "WHERE status = 'active'"
        ).fetchone()
        assert tuple(manifest) == ("sqlite-bruteforce", 3072)
    finally:
        plugin.shutdown()



def test_scope_recall_package_import_is_light_without_hermes_runtime(monkeypatch):
    monkeypatch.delitem(sys.modules, "scope_recall", raising=False)
    monkeypatch.delitem(sys.modules, "agent.memory_provider", raising=False)
    plugin_root = str(PLUGIN_ROOT)
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT.parent))

    class _BlockHermesRuntimeImport:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "agent.memory_provider":
                raise ModuleNotFoundError("Hermes runtime intentionally unavailable")
            return None

    blocker = _BlockHermesRuntimeImport()
    sys.meta_path.insert(0, blocker)
    try:
        module = importlib.import_module("scope_recall")
    finally:
        sys.meta_path.remove(blocker)
        restored_package = types.ModuleType(PACKAGE_NAME)
        restored_package.__path__ = [str(PLUGIN_ROOT)]
        monkeypatch.setitem(sys.modules, PACKAGE_NAME, restored_package)

    assert list(getattr(module, "__path__", [])) == [plugin_root]
    assert module.__all__ == ["register"]
    assert callable(module.register)



def test_openclaw_import_script_is_idempotent(tmp_path):
    source_dir = tmp_path / "openclaw-memory"
    source_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(source_dir))
    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), 4)),
            pa.field("category", pa.string()),
            pa.field("scope", pa.string()),
            pa.field("importance", pa.float32()),
            pa.field("timestamp", pa.int64()),
            pa.field("metadata", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(
        [
            {
                "id": "legacy-1",
                "text": "Use uv run app for deploys.",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "category": "memory",
                "scope": "joy",
                "importance": 0.8,
                "timestamp": 1715472000000,
                "metadata": json.dumps({"source": "test"}, ensure_ascii=False),
            }
        ],
        schema=schema,
    )
    db.create_table("memories", data=table)

    hermes_home = tmp_path / "hermes-home"
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--source",
        str(source_dir),
        "--hermes-home",
        str(hermes_home),
    ]
    dry_run = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    assert dry_run["ok"] is True
    assert dry_run["dry_run"] is True
    assert dry_run["rows_inserted"] == 0
    assert not (hermes_home / "scope-recall" / "memory.sqlite3").exists()

    apply_cmd = [*cmd, "--apply"]
    first = json.loads(subprocess.run(apply_cmd, check=True, capture_output=True, text=True).stdout)
    second = json.loads(subprocess.run(apply_cmd, check=True, capture_output=True, text=True).stdout)

    assert first["ok"] is True
    assert first["rows_inserted"] == 1
    assert first["rows_skipped"] == 0
    assert second["ok"] is True
    assert second["rows_inserted"] == 0
    assert second["rows_skipped"] == 1
    assert second["idempotent"] is True

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    try:
        memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        ledger_count = conn.execute("SELECT COUNT(*) FROM import_ledger").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
    finally:
        conn.close()

    assert memory_count == 1
    assert ledger_count == 1
    assert fts_count == 1


def test_lexical_and_combined_scores_are_capped_at_one():
    from scope_recall.scoring import combine_scores, lexical_score

    lexical = lexical_score(
        query="Joy prefers concise answers",
        content="Joy prefers concise answers with direct problem-first reporting.",
        summary="Joy prefers concise answers",
        source="builtin-curated",
        target="user",
    )
    assert 0.0 <= lexical <= 1.0

    combined = combine_scores(
        {"lexical_score": 1.3, "vector_score": 1.2},
        lexical_weight=0.45,
        vector_weight=0.55,
    )
    assert combined == 1.0


def test_recall_merge_preserves_incoming_recency_metadata(tmp_path, monkeypatch):
    from scope_recall.models import RecallItem

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-recency-merge",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        plugin._retrieval_config = {"mode": "hybrid", "min_score": 0.0, "candidate_pool": 3, "fusion_strategy": "linear", "entity_distance_weight": 0.0}
        duplicate_content = "Joy prefers concise answers with direct problem-first reporting."
        older = RecallItem(
            id="older",
            source="tool",
            target="user",
            content=duplicate_content,
            summary=duplicate_content,
            updated_at="2026-01-01T00:00:00+00:00",
            score=0.4,
            metadata={"lexical_score": 0.4, "base_score": 0.4, "recency_bonus": 0.05},
        )
        newer = RecallItem(
            id="newer",
            source="tool",
            target="user",
            content=duplicate_content,
            summary=duplicate_content,
            updated_at="2026-01-02T00:00:00+00:00",
            score=0.7,
            metadata={"vector_score": 0.7, "base_score": 0.7, "recency_bonus": 0.25},
        )

        plugin._search_db_memories = lambda query, limit: [older]
        plugin._search_vector_memories = lambda query, limit: [newer]
        plugin._search_curated_memories = lambda query: []
        recall_module = importlib.import_module(
            plugin._recall_service.__class__.__module__
        )
        monkeypatch.setattr(
            recall_module,
            "memory_freshness_map",
            lambda _conn, ids: {
                memory_id: {
                    "subject_id": memory_id,
                    "truth_type": "factual",
                    "status": "current",
                }
                for memory_id in ids
            },
        )

        results = plugin._recall_service.search_memories("Joy concise answers", limit=1)
    finally:
        plugin.shutdown()

    assert len(results) == 1
    assert results[0].id == "newer"
    assert results[0].metadata["lexical_score"] == 0.4
    assert results[0].metadata["vector_score"] == 0.7
    assert results[0].metadata["base_score"] == pytest.approx(0.565)
    assert results[0].metadata["recency_bonus"] == 0.25


def test_openai_compatible_embedder_rotates_to_next_key_after_failure(monkeypatch):
    from scope_recall.embedders import OpenAICompatibleEmbedder

    attempts: list[str] = []
    encoding_formats: list[str | None] = []
    constructed_max_retries: list[int] = []
    constructed_timeouts: list[object] = []

    class _FakeEmbeddings:
        def __init__(self, key: str) -> None:
            self.key = key

        def create(
            self,
            *,
            model: str,
            input: list[str],
            encoding_format: str | None = None,
            timeout: object = None,
        ):
            attempts.append(self.key)
            encoding_formats.append(encoding_format)
            if self.key == "public-test-key-1":
                raise RuntimeError("simulated exhausted key")

            class _Item:
                embedding = [0.1, 0.2, 0.3]

            class _Response:
                data = [_Item() for _ in input]

            return _Response()

    class _FakeOpenAI:
        def __init__(
            self,
            *,
            api_key: str,
            base_url: str | None = None,
            http_client=None,
            timeout: object = None,
            max_retries: int = 0,
        ) -> None:  # noqa: ANN001,ARG002
            constructed_max_retries.append(max_retries)
            constructed_timeouts.append(timeout)
            self.embeddings = _FakeEmbeddings(api_key)
            self.timeout = timeout
            self.max_retries = max_retries

    monkeypatch.setattr("scope_recall.embedders.OpenAI", _FakeOpenAI)
    embedder = OpenAICompatibleEmbedder(
        model="gemini-embedding-001",
        api_key=["public-test-key-1", "public-test-key-2"],
        base_url="https://example.invalid/v1",
        dimensions=3,
    )

    try:
        vectors = embedder.embed_texts(["memory row"])

        assert vectors == [[0.1, 0.2, 0.3]]
        assert attempts == ["public-test-key-1", "public-test-key-2"]
        assert encoding_formats == ["float", "float"]
        assert constructed_max_retries == [0, 0]
        assert all(item is not None for item in constructed_timeouts)
    finally:
        embedder.close()


def test_release_runner_returns_structured_error_when_executable_missing() -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_missing_prereq"
    )

    result = release_check.run(["definitely-missing-release-prereq-x9q7"])

    assert result["returncode"] != 0
    assert result["error"] == "prerequisite_missing"
    assert result["prerequisite"] == "definitely-missing-release-prereq-x9q7"
    assert result["stdout"] == ""
    assert result["stderr"] == ""



def test_release_runner_discovers_instance_bundled_git_when_path_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_bundled_git"
    )
    source_root = tmp_path / "workspace" / "tmp" / "scope-recall-candidate"
    source_root.mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("instance: test\n", encoding="utf-8")
    bundled_git = tmp_path / "git" / "cmd" / "git.exe"
    bundled_git.parent.mkdir(parents=True)
    bundled_git.write_bytes(b"")
    monkeypatch.setattr(release_check.shutil, "which", lambda *_args, **_kwargs: None)

    resolved = release_check.resolve_release_command(
        ["git", "status", "--porcelain=v1"],
        env={"PATH": ""},
        root=source_root,
    )

    assert resolved[0] == str(bundled_git)
    assert resolved[1:] == ["status", "--porcelain=v1"]


def test_release_runner_never_executes_git_from_candidate_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A candidate-controlled git.exe must not outrank the instance bundle."""

    release_check = _load_release_check_module(
        "scope_recall_check_release_rejects_candidate_git"
    )
    source_root = tmp_path / "workspace" / "tmp" / "scope-recall-candidate"
    source_root.mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("instance: test\n", encoding="utf-8")
    forged_git = source_root / "git" / "cmd" / "git.exe"
    forged_git.parent.mkdir(parents=True)
    forged_git.write_bytes(b"candidate-controlled")
    bundled_git = tmp_path / "git" / "cmd" / "git.exe"
    bundled_git.parent.mkdir(parents=True)
    bundled_git.write_bytes(b"trusted-instance-bundle")
    monkeypatch.setattr(
        release_check.shutil,
        "which",
        lambda *_args, **_kwargs: str(forged_git),
    )

    resolved = release_check.resolve_release_command(
        ["git", "status", "--porcelain=v1"],
        env={"PATH": ""},
        root=source_root,
    )

    assert resolved[0] == str(bundled_git)
    assert resolved[0] != str(forged_git)


def test_release_runner_fails_closed_when_only_candidate_git_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject a candidate Git instead of returning a bare executable name."""

    release_check = _load_release_check_module(
        "scope_recall_check_release_rejects_only_candidate_git"
    )
    source_root = tmp_path / "scope-recall-candidate"
    source_root.mkdir()
    forged_git = source_root / "git.exe"
    forged_git.write_bytes(b"candidate-controlled")
    monkeypatch.setattr(
        release_check.shutil,
        "which",
        lambda *_args, **_kwargs: str(forged_git),
    )

    with pytest.raises(FileNotFoundError, match="trusted Git executable"):
        release_check.resolve_release_command(
            ["git", "status", "--porcelain=v1"],
            env={"PATH": str(source_root)},
            root=source_root,
        )


def test_release_gate_fails_closed_without_traceback_when_git_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_git_prereq"
    )
    candidate = tmp_path / "scope-recall-candidate"
    candidate.mkdir()
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setattr(release_check, "ROOT", candidate)
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.setattr(release_check.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["check.release.py"])

    exit_code = release_check.main()
    captured = capsys.readouterr()

    assert exit_code != 0
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    serialized = json.dumps(payload)
    assert "prerequisite" in serialized
    assert "git" in serialized.lower()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_release_benchmark_gate_accepts_lexical_50k_release_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_lexical_50k_contract"
    )

    golden_payload = {
        "schema_version": "golden_benchmark_report.v1",
        "passed": True,
        "golden_name": "curated_recall_regression_v2",
        "query_count": 120,
        "failures": [],
        "metrics": {},
    }
    smoke_payload = {
        "schema_version": "golden_benchmark_report.v1",
        "passed": True,
        "golden_name": "recall_smoke_hybrid_vector_v1",
        "query_count": 5,
        "failures": [],
        "metrics": {},
    }
    golden_by_cases_path = {
        "": golden_payload,
        "benchmarks/golden_recall_hybrid_cases.json": smoke_payload,
    }
    monkeypatch.setattr(
        release_check,
        "_run_golden_benchmark",
        lambda cases_path: (
            {"returncode": 0, "stdout": json.dumps(golden_by_cases_path[cases_path]), "stderr": ""},
            golden_by_cases_path[cases_path],
        ),
    )

    json_payloads = {
        "scripts/benchmark.memory_evolution.py": {
            "schema_version": "scope-recall.memory-evolution-benchmark.v1",
            "passed": True,
            "metrics": {},
        },
        "scripts/benchmark.reflection.py": {
            "schema_version": "scope-recall.reflection-benchmark.v3",
            "passed": True,
            "metrics": {},
        },
        "scripts/benchmark.temporal_scale.py": {
            "schema_version": "scope-recall.temporal-scale.v2",
            "candidate_version": release_check.PACKAGE_VERSION,
            "passed": True,
            "sizes": [100_000, 1_000_000],
            "rounds_per_query": 30,
        },
        "scripts/benchmark.lexical_cjk.py": _valid_lexical_v2_payload(),
    }

    def fake_json_benchmark(script_path: str):
        payload = json_payloads[script_path]
        return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}, payload

    monkeypatch.setattr(release_check, "_run_json_benchmark", fake_json_benchmark)
    monkeypatch.setattr(
        release_check,
        "run",
        lambda cmd, **kwargs: {
            "cmd": cmd,
            "returncode": 0,
            "stdout": json.dumps({"benchmark_name": "graph_relation_rerank_v1", "passed": True, "metrics": {}}),
            "stderr": "",
        },
    )

    result = release_check.benchmark_check()

    assert result["ok"] is True, result
    assert result["lexical_cjk_metrics"]["rows"] == 50_000
    assert result["lexical_cjk_metrics"]["rounds"] == 20
    assert result["lexical_cjk_metrics"]["target_misses"] == ["shadow_p95"]

    temporal_scale_payload = json_payloads["scripts/benchmark.temporal_scale.py"]
    temporal_scale_payload["candidate_version"] = "0.0.0"
    wrong_candidate_version = release_check.benchmark_check()
    assert wrong_candidate_version["ok"] is False
    temporal_scale_payload["candidate_version"] = release_check.PACKAGE_VERSION

    lexical_payload = json_payloads["scripts/benchmark.lexical_cjk.py"]
    lexical_payload["shadow_p95_ms"] = "fast"
    invalid_latency = release_check.benchmark_check()
    assert invalid_latency["ok"] is False

    lexical_payload["shadow_p95_ms"] = 128.146864
    lexical_payload["shadow_to_legacy_p95_ratio"] = 4.01
    relative_regression = release_check.benchmark_check()
    assert relative_regression["ok"] is False

    lexical_payload["shadow_to_legacy_p95_ratio"] = 1.407231
    lexical_payload["page_growth_ratio"] = 2.51
    page_growth_regression = release_check.benchmark_check()
    assert page_growth_regression["ok"] is False


def _valid_lexical_v2_payload() -> dict[str, object]:
    return {
        "schema_version": "scope-recall.lexical-cjk-benchmark.v2",
        "passed": True,
        "contract_mode": "release",
        "rows": 50_000,
        "rounds": 20,
        "limit": 10,
        "cjk_queries": 3,
        "cjk_expected_found": 3,
        "english_queries": 2,
        "english_regressions": 0,
        "max_result_count": 10,
        "legacy_p50_ms": 54.4766,
        "legacy_p95_ms": 91.063128,
        "shadow_p50_ms": 98.043524,
        "shadow_p95_ms": 128.146864,
        "shadow_to_legacy_p95_ratio": 1.407231,
        "baseline_pages": 10_786,
        "shadow_pages": 19_295,
        "page_growth_ratio": 1.788893,
        "targets": {"shadow_p95_ms": 100.0},
        "target_misses": ["shadow_p95"],
        "budgets": {
            "shadow_to_legacy_p95_ratio_max": 4.0,
            "page_growth_ratio_max": 2.5,
        },
        "failures": [],
    }


def test_release_lexical_v2_payload_validation_fails_closed() -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_lexical_v2_adversarial"
    )

    valid = _valid_lexical_v2_payload()
    assert release_check.validate_lexical_benchmark_payload(valid) is True

    bad_payloads: list[tuple[str, dict[str, object]]] = []

    def changed(name: str, **updates: object) -> None:
        payload = _valid_lexical_v2_payload()
        payload.update(updates)
        bad_payloads.append((name, payload))

    changed("negative_ratio", shadow_to_legacy_p95_ratio=-0.1)
    changed("negative_page_growth", page_growth_ratio=-0.1)
    changed(
        "inconsistent_ratio",
        legacy_p95_ms=1.0,
        shadow_p95_ms=100.0,
        shadow_to_legacy_p95_ratio=1.0,
        target_misses=[],
    )
    changed(
        "obsolete_fast_host_ratio",
        legacy_p50_ms=0.9,
        legacy_p95_ms=1.3848,
        shadow_p50_ms=30.0,
        shadow_p95_ms=81.7871,
        shadow_to_legacy_p95_ratio=59.060586,
        target_misses=[],
    )
    changed(
        "fast_host_target_regression",
        legacy_p50_ms=0.9,
        legacy_p95_ms=1.3848,
        shadow_p50_ms=30.0,
        shadow_p95_ms=100.1,
        shadow_to_legacy_p95_ratio=4.004,
        target_misses=["shadow_p95"],
    )
    changed("inconsistent_page_growth", page_growth_ratio=1.0)
    changed("missing_target_miss", target_misses=[])
    changed("spurious_target_miss_when_met", shadow_p95_ms=99.0, target_misses=["shadow_p95"], shadow_to_legacy_p95_ratio=1.087231)
    changed("unknown_target_miss", target_misses=["shadow_p50"])
    changed("target_misses_not_list", target_misses="shadow_p95")
    changed("passed_string_false", passed="false")
    changed("smoke_contract_mode", contract_mode="smoke")
    changed("duplicate_target_miss", target_misses=["shadow_p95", "shadow_p95"])
    changed("nan_latency", shadow_p95_ms=float("nan"))
    changed("infinite_latency", shadow_p95_ms=float("inf"))
    changed("too_many_rounds", rounds=101)
    changed("insufficient_release_rounds", rounds=19)
    changed("zero_results", max_result_count=0)
    changed(
        "zero_latency",
        legacy_p50_ms=0.0,
        legacy_p95_ms=0.0,
        shadow_p50_ms=0.0,
        shadow_p95_ms=0.0,
        shadow_to_legacy_p95_ratio=0.0,
        target_misses=[],
    )
    changed("unknown_top_level_field", extension_field=True)
    changed("oversized_integer", legacy_p95_ms=10**1000)
    changed(
        "contradictory_target_evidence",
        targets={"shadow_p95_ms": 50.0},
    )

    missing_failures = _valid_lexical_v2_payload()
    del missing_failures["failures"]
    bad_payloads.append(("missing_failures", missing_failures))

    unhashable_target_miss = _valid_lexical_v2_payload()
    unhashable_target_miss["target_misses"] = [{"bad": 1}]
    bad_payloads.append(("unhashable_target_miss", unhashable_target_miss))

    for name, payload in bad_payloads:
        assert release_check.validate_lexical_benchmark_payload(payload) is False, name


def test_release_lexical_v2_absolute_shadow_p95_target_miss_is_not_hard_gate() -> None:
    """RB-6: absolute shadow_p95<=100 is a cross-host target, not a hard gate."""

    release_check = _load_release_check_module(
        "scope_recall_check_release_lexical_v2_rb6_target"
    )

    absolute_miss = _valid_lexical_v2_payload()
    assert absolute_miss["shadow_p95_ms"] > absolute_miss["targets"]["shadow_p95_ms"]
    assert absolute_miss["target_misses"] == ["shadow_p95"]
    assert absolute_miss["shadow_to_legacy_p95_ratio"] <= 4.0
    assert absolute_miss["page_growth_ratio"] <= 2.5
    assert absolute_miss["cjk_expected_found"] == absolute_miss["cjk_queries"]
    assert absolute_miss["english_regressions"] == 0
    assert absolute_miss["max_result_count"] <= absolute_miss["limit"]
    assert release_check.validate_lexical_benchmark_payload(absolute_miss) is True


def test_release_lexical_v2_accepts_target_bounded_fast_host_payload() -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_lexical_v2_fast_host"
    )

    payload = _valid_lexical_v2_payload()
    payload.update(
        {
            "legacy_p50_ms": 0.9,
            "legacy_p95_ms": 1.3848,
            "shadow_p50_ms": 30.0,
            "shadow_p95_ms": 81.7871,
            "shadow_to_legacy_p95_ratio": 3.271484,
            "target_misses": [],
        }
    )

    assert release_check.validate_lexical_benchmark_payload(payload) is True


def test_release_lexical_v2_accepts_producer_rounding_boundaries() -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_lexical_v2_rounding"
    )

    target_boundary = _valid_lexical_v2_payload()
    target_boundary.update(
        {
            "legacy_p50_ms": 90.0,
            "legacy_p95_ms": 100.0,
            "shadow_p50_ms": 90.0,
            "shadow_p95_ms": 100.0,
            "shadow_to_legacy_p95_ratio": 1.0,
            "target_misses": [],
        }
    )
    ratio_rounding_boundary = _valid_lexical_v2_payload()
    ratio_rounding_boundary.update(
        {
            "legacy_p50_ms": 0.4,
            "legacy_p95_ms": 0.500789,
            "shadow_p50_ms": 0.4,
            "shadow_p95_ms": 0.410686,
            "shadow_to_legacy_p95_ratio": 0.016427,
            "target_misses": [],
        }
    )

    assert release_check.validate_lexical_benchmark_payload(target_boundary) is True
    assert (
        release_check.validate_lexical_benchmark_payload(ratio_rounding_boundary)
        is True
    )
