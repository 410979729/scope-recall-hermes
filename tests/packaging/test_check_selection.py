from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402


def test_write_scope_delete_change_adds_safety_baseline() -> None:
    selected, details = check.select_tests(
        "contract",
        changed=["core/delete_storage.py", "core/visibility.py"],
    )

    assert "I01_I03_I04_I06_I07_I08_I09_I10_I14_safety_baseline" in details["reasons"]
    assert set(check.CONTRACT_I_BASELINE) <= set(selected)
    assert "tests/contract/test_v11_deletion.py" in selected


def test_unknown_change_requires_integration_instead_of_empty_green() -> None:
    selected, details = check.select_tests("unit", changed=["new_dependency_adapter.py"])

    assert selected
    assert details["unknown_files"] == ["new_dependency_adapter.py"]
    assert "integration" in details["required_tiers"]
    assert "packaging" in details["required_tiers"]
    assert "unknown_dependency_requires_conservative_integration_and_build" in details["reasons"]


def test_packaging_change_requires_build_and_integration() -> None:
    selected, details = check.select_tests("unit", changed=["pyproject.toml"])

    assert set(check.SUITES["unit"]) <= set(selected)
    assert set(check.SUITES["integration"]) <= set(selected)
    assert set(check.SUITES["packaging"]) <= set(selected)
    assert details["required_tiers"] == ["unit", "integration", "packaging"]
    assert "integration_and_build_for_packaging_or_config" in details["reasons"]


def test_release_uses_the_same_packaging_helper_authorization() -> None:
    packaging = check.packaging_helper_env("packaging")
    release = check.packaging_helper_env("release")
    assert packaging == release
    assert "SCOPE_RECALL_TEST_PACKAGING_HELPER_ROOTS" in packaging
    helper = Path(packaging["SCOPE_RECALL_TEST_PACKAGING_HELPER_ROOTS"])
    assert helper.is_dir()
    assert check.packaging_helper_env("native") == {}
    assert check.packaging_helper_env("unit") == {}


def test_release_selection_exposes_model_gate_without_claiming_pass() -> None:
    selected, details = check.select_tests("release")
    payload = check._selection_output("release", selected, details, status="planned_not_executed")

    assert selected
    assert {"native", "hermes", "codex", "migration", "clean_wheel", "model"} >= set(
        payload["required_gates"] + payload["missing_gates"]
    )
    assert payload["missing_gates"] == payload["required_gates"]


def test_changed_contract_file_is_always_selected() -> None:
    selected, details = check.select_tests(
        "unit", changed=["tests/contract/test_p09_recall_packet.py"]
    )

    assert "tests/contract/test_p09_recall_packet.py" in selected
    assert details["unknown_files"] == []


def test_claim_change_adds_current_history_time_and_migration_closure() -> None:
    selected, details = check.select_tests("unit", changed=["core/claims.py"])

    assert set(check.CLAIMS_TIME_CONTRACT_CLOSURE) <= set(selected)
    assert "tests/migration/test_v11_migration.py" not in selected
    assert "migration" in details["required_tiers"]
    assert "claims_time_current_history_candidate_and_migration_closure" in details["reasons"]


def test_host_change_targets_only_changed_host_and_core_baseline() -> None:
    selected, details = check.select_tests(
        "unit", changed=["adapters/codex/mcp_server.py"]
    )

    assert set(check.CODEX_HOST_TESTS) <= set(selected)
    assert not set(check.HERMES_HOST_TESTS) & set(selected)
    assert set(check.SAFETY_BASELINE) <= set(selected)
    assert "codex" in details["required_tiers"]


def test_hermes_identity_change_selects_direct_identity_contract() -> None:
    selected, details = check.select_tests(
        "unit", changed=["adapters/hermes/identity.py"]
    )

    assert "tests/host/hermes/test_identity.py" in selected
    assert "tests/host/hermes/test_audience_isolation.py" in selected
    assert "tests/host/hermes/test_dedupe.py" in selected
    assert "hermes" in details["required_tiers"]


def test_integration_and_release_include_new_runtime_and_core_contracts() -> None:
    integration, _ = check.select_tests("integration")
    release, _ = check.select_tests("release")

    assert set(check.CORE_RELEASE_CONTRACTS) <= set(integration)
    assert set(check.RUNTIME_BOUNDARY_TESTS) <= set(integration)
    assert set(check.SCRIPT_GATE_TESTS) <= set(integration)
    assert "tests/contract/test_auto_query_echo.py" in integration
    assert "tests/host/codex/test_lifecycle_worker_wakeup.py" in integration
    assert set(check.CORE_RELEASE_CONTRACTS) <= set(release)
    assert set(check.RUNTIME_BOUNDARY_TESTS) <= set(release)
    assert set(check.SCRIPT_GATE_TESTS) <= set(release)
    assert "tests/contract/test_v11_vector_timeout_fallback.py" in integration
    assert "tests/contract/test_v11_vector_timeout_fallback.py" in release


def test_retrieval_selector_keeps_vector_timeout_fallback() -> None:
    selected, _ = check.select_tests("retrieval")
    assert "tests/contract/test_v11_vector_timeout_fallback.py" in selected


def test_packaging_includes_script_gate_baseline() -> None:
    packaging, _ = check.select_tests("packaging")

    assert set(check.SCRIPT_GATE_TESTS) <= set(packaging)
    assert "tests/contract/test_auto_query_echo.py" not in packaging


def test_clean_integration_keeps_native_host_and_migration_baseline() -> None:
    selected, details = check.select_tests("integration", changed=[])

    assert set(check.SUITES["native"]) <= set(selected)
    assert set(check.SUITES["host"]) <= set(selected)
    assert set(check.SUITES["migration"]) <= set(selected)
    assert "tests/host/hermes/test_operator_tools.py" in selected
    assert "tests/host/hermes/test_reinjection.py" in selected
    assert "tests/host/test_runtime_config_threshold.py" in selected
    assert "tests/contract/test_p13_operator_retry.py" in selected
    assert "tests/contract/test_p13_configurable_budget.py" in selected
    assert "native" in details["required_tiers"]
    assert "hermes" in details["required_tiers"]
    assert "codex" in details["required_tiers"]
    assert "migration" in details["required_tiers"]
    assert "integration_stable_native_host_migration_baseline" in details["reasons"]


def test_process_tiers_are_explicitly_distinct_from_guarded_contract_tiers() -> None:
    assert "tests/contract/test_runtime_worker_entry.py" in check.SUITES["integration"]
    assert "tests/contract/test_runtime_worker_entry.py" not in check.SUITES["contract"]


def test_eval_without_authorization_is_nonzero_and_model_free(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["check.py", "--tier", "eval"])

    assert check.main() != 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "model_calls_disabled"
    assert output["model_calls"] is True
    assert output["missing_gates"] == ["explicit_model_authorization"]


# --------------------------------------------------------------------------
# The suite watchdog bounds a hang, not a growing suite
# --------------------------------------------------------------------------

def test_release_keeps_its_own_fixed_budget() -> None:
    assert check.pytest_watchdog_seconds("release", 1) == check.RELEASE_WATCHDOG_SECONDS
    assert check.pytest_watchdog_seconds("release", 999) == check.RELEASE_WATCHDOG_SECONDS


def test_a_small_selection_keeps_the_historical_floor() -> None:
    assert check.pytest_watchdog_seconds("contract", 0) == check.DEFAULT_WATCHDOG_SECONDS
    assert check.pytest_watchdog_seconds("contract", 1) > check.DEFAULT_WATCHDOG_SECONDS


def test_a_bigger_selection_gets_more_room() -> None:
    """The integration tier grew past a flat 180s without anything hanging."""
    small = check.pytest_watchdog_seconds("integration", 10)
    large = check.pytest_watchdog_seconds("integration", 60)
    assert large > small >= check.DEFAULT_WATCHDOG_SECONDS


def test_the_real_integration_selection_is_not_at_its_own_limit() -> None:
    """The regression this replaced: the gate timed out instead of reporting."""
    selected, _details = check.select_tests("integration", changed=[])
    budget = check.pytest_watchdog_seconds("integration", len(selected))
    assert budget >= 2 * check.DEFAULT_WATCHDOG_SECONDS, (
        f"{len(selected)} files share a {budget}s budget; the suite already "
        "needed more than 180s to finish")


def test_no_run_can_wait_longer_than_the_release_budget() -> None:
    assert check.pytest_watchdog_seconds("integration", 10_000) == check.RELEASE_WATCHDOG_SECONDS


def test_a_nonsense_count_falls_back_to_the_floor() -> None:
    for bad in (None, -1, "12", 1.5, True):
        assert check.pytest_watchdog_seconds("integration", bad) == check.DEFAULT_WATCHDOG_SECONDS


# --------------------------------------------------------------------------
# Release states the precondition it depends on
# --------------------------------------------------------------------------

def test_this_interpreter_reports_its_own_installation_state() -> None:
    """Importable is not installed: the gate puts the source tree on
    PYTHONPATH, which ``importlib.metadata`` cannot see."""
    import importlib.metadata

    try:
        importlib.metadata.version("hermes-scope-recall")
        installed = True
    except importlib.metadata.PackageNotFoundError:
        installed = False
    assert check.distribution_is_installed() is installed


def test_an_interpreter_that_does_not_exist_is_not_installed() -> None:
    assert check.distribution_is_installed("F:/nonexistent-interpreter/python.exe") is False


def test_release_refuses_early_with_a_named_gap_and_a_remedy(monkeypatch, capsys) -> None:
    """The regression this replaced: a fresh checkout failed release on
    ``assert 'entry_point_missing' == 'host_config_missing'``, which names
    neither the cause nor the cure."""
    monkeypatch.setattr(check, "distribution_is_installed", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["check.py", "--tier", "release"])

    assert check.main() != 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "distribution_not_installed"
    assert output["missing_gates"] == ["installed_distribution"]
    assert "pip install" in output["remedy"]


def test_other_tiers_do_not_require_an_installed_distribution(monkeypatch) -> None:
    """Only release asks the doctor to probe a real entry point."""
    monkeypatch.setattr(check, "distribution_is_installed", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["check.py", "--tier", "contract", "--plan"])

    assert check.main() == 0
