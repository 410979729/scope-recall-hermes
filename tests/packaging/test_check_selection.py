from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
    assert "tests/contract/test_deletion.py" in selected


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


def test_a_release_plan_names_its_gates_without_claiming_them() -> None:
    selected, details = check.select_tests("release")
    payload = check._selection_output("release", selected, details, status="planned_not_executed")

    assert selected
    assert payload["required_gates"] == ["native", "hermes", "codex", "migration", "clean_wheel"]
    assert payload["missing_gates"] == payload["required_gates"]
    assert "model" not in payload["required_gates"], "the P18 model gate was removed in 3.4.0rc6"


def test_changed_contract_file_is_always_selected() -> None:
    selected, details = check.select_tests("unit", changed=["tests/contract/test_recall_packet_compiler.py"])

    assert "tests/contract/test_recall_packet_compiler.py" in selected
    assert details["unknown_files"] == []


def test_claim_change_adds_current_history_time_and_migration_closure() -> None:
    selected, details = check.select_tests("unit", changed=["core/claims.py"])

    assert set(check.CLAIMS_TIME_CONTRACT_CLOSURE) <= set(selected)
    assert "tests/migration/test_migration_drills.py" not in selected
    assert "migration" in details["required_tiers"]
    assert "claims_time_current_history_candidate_and_migration_closure" in details["reasons"]


def test_host_change_targets_only_changed_host_and_core_baseline() -> None:
    selected, details = check.select_tests("packaging", changed=["adapters/clients/mcp_server.py"])

    assert set(check.CODEX_HOST_TESTS) <= set(selected)
    assert not set(check.HERMES_HOST_TESTS) & set(selected)
    assert set(check.SAFETY_BASELINE) <= set(selected)
    assert "host" in details["required_tiers"]
    assert "host_specific_contract_and_core_safety_baseline" in details["reasons"]


def test_hermes_identity_change_selects_direct_identity_contract() -> None:
    selected, details = check.select_tests("packaging", changed=["adapters/hermes/identity.py"])

    assert "tests/host/hermes/test_identity.py" in selected
    assert "tests/host/hermes/test_audience_isolation.py" in selected
    assert "tests/host/hermes/test_dedupe.py" in selected
    assert "host" in details["required_tiers"]


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
    assert "tests/contract/test_vector_timeout_fallback.py" in integration
    assert "tests/contract/test_vector_timeout_fallback.py" in release


def test_retrieval_selector_keeps_vector_timeout_fallback() -> None:
    selected, _ = check.select_tests("retrieval")
    assert "tests/contract/test_vector_timeout_fallback.py" in selected


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
    assert "tests/contract/test_operator_retry.py" in selected
    assert "tests/contract/test_recall_budget_config.py" in selected
    assert "native" in details["required_tiers"]
    assert "host" in details["required_tiers"]
    assert "migration" in details["required_tiers"]
    assert "integration_stable_native_host_migration_baseline" in details["reasons"]


def test_process_tiers_are_explicitly_distinct_from_guarded_contract_tiers() -> None:
    assert "tests/contract/test_runtime_worker_entry.py" in check.SUITES["integration"]
    assert "tests/contract/test_runtime_worker_entry.py" not in check.SUITES["contract"]


# --------------------------------------------------------------------------
# A run selects only the tests its tier's process policy allows
# --------------------------------------------------------------------------

#: The tiers whose runs refuse child processes and sockets (``tests/v11_guard.py``).
IN_PROCESS_TIERS = sorted(set(check.SUITES) - check.PROCESS_TIERS)

#: Changed files that between them reach every addition the selector makes.
CHANGES = {
    "client": ["adapters/clients/mcp_server.py"],
    "hermes": ["adapters/hermes/identity.py"],
    "runtime": ["runtime/worker_entry.py"],
    "vector": ["runtime/lance_port.py"],
    "store": ["core/delete_storage.py", "core/visibility.py"],
    "claim": ["core/claims.py"],
    "recall": ["core/recall.py"],
    "packaging": ["pyproject.toml"],
    "unknown": ["new_dependency_adapter.py"],
    "selector": ["scripts/check.py"],
    "host test": ["tests/host/codex/test_remote.py"],
    "contract test": ["tests/contract/test_storage.py"],
}

#: What a change calls for beyond a tier's own suite and some of whose tests need a process, with the tiers that run it.
PROCESS_ADDITIONS = {
    "client": (CHANGES["client"], check.CODEX_HOST_TESTS, ["host"]),
    "hermes": (CHANGES["hermes"], check.HERMES_HOST_TESTS, ["host"]),
    "runtime": (
        CHANGES["runtime"],
        [*check.RUNTIME_BOUNDARY_TESTS, *check.HERMES_HOST_TESTS, *check.CODEX_HOST_TESTS],
        ["host", "integration"],
    ),
    "vector": (CHANGES["vector"], [*check.SUITES["native"], *check.I_CORE_BASELINE], ["native"]),
    "packaging": (
        CHANGES["packaging"],
        [*check.SUITES["integration"], *check.SUITES["packaging"]],
        ["integration", "packaging"],
    ),
    "unknown": (
        CHANGES["unknown"],
        [*check.SUITES["integration"], *check.SUITES["packaging"]],
        ["integration", "packaging"],
    ),
}


def run_only_by_process_tiers(tests: list[str]) -> set[str]:
    """The ``tests`` that no tier refusing child processes runs on its own: not in its suite, not in the baseline built
    for those tiers."""
    in_process = {path for tier in IN_PROCESS_TIERS for path in check.SUITES[tier]} | set(check.CONTRACT_I_BASELINE)
    return set(tests) - in_process


@pytest.mark.parametrize("tier", sorted(check.SUITES))
@pytest.mark.parametrize("changed", CHANGES.values(), ids=list(CHANGES))
def test_a_plan_names_only_tiers_that_exist(tier, changed) -> None:
    """``--tier`` accepts the keys of ``SUITES`` and nothing else, so any other name is a tier nobody can run."""
    _, details = check.select_tests(tier, changed=changed)

    assert set(details["required_tiers"]) <= set(check.SUITES)


def test_every_tier_a_plan_names_is_a_tier_the_runner_accepts(monkeypatch, capsys) -> None:
    changed = [path for change in ("client", "vector", "packaging", "claim") for path in CHANGES[change]]
    monkeypatch.setattr(sys, "argv", ["check.py", "--tier", "unit", "--plan", "--changed-files", *changed])
    assert check.main() == 0
    named = json.loads(capsys.readouterr().out)["selection"]["required_tiers"]

    assert len(named) > 1
    for name in named:
        monkeypatch.setattr(sys, "argv", ["check.py", "--tier", name, "--plan"])
        assert check.main() == 0, name
        capsys.readouterr()


@pytest.mark.parametrize("tier", IN_PROCESS_TIERS)
@pytest.mark.parametrize("change", ["client", "hermes", "runtime"])
def test_a_host_change_selects_no_host_test_where_child_processes_are_refused(tier, change) -> None:
    selected, details = check.select_tests(tier, changed=CHANGES[change])

    assert not set(selected) & set(check.SUITES["host"])
    assert set(check.SAFETY_BASELINE) <= set(selected)
    assert "host" in details["required_tiers"]
    assert "core_safety_baseline_for_host_change" in details["reasons"]
    assert "host_specific_contract_and_core_safety_baseline" not in details["reasons"]


@pytest.mark.parametrize("tier", IN_PROCESS_TIERS)
@pytest.mark.parametrize("change", PROCESS_ADDITIONS)
def test_a_tier_that_refuses_child_processes_leaves_their_tests_to_the_tier_that_runs_them(tier, change) -> None:
    changed, additions, named = PROCESS_ADDITIONS[change]

    selected, details = check.select_tests(tier, changed=changed)

    assert not run_only_by_process_tiers(additions) & (set(selected) - set(check.SUITES[tier]))
    assert set(named) <= set(details["required_tiers"])


@pytest.mark.parametrize("tier", sorted(check.PROCESS_TIERS))
@pytest.mark.parametrize("change", PROCESS_ADDITIONS)
def test_a_process_tier_selects_the_tests_a_change_calls_for(tier, change) -> None:
    changed, additions, named = PROCESS_ADDITIONS[change]

    selected, details = check.select_tests(tier, changed=changed)

    assert set(additions) <= set(selected)
    assert set(named) <= set(details["required_tiers"])


def test_a_changed_test_is_left_to_the_process_tier_that_owns_it() -> None:
    changed = CHANGES["host test"]

    selected, details = check.select_tests("unit", changed=changed)

    assert changed[0] not in selected
    assert details["required_tiers"] == ["unit", "host"]
    assert "changed_tests_owned_by_process_tiers" in details["reasons"]
    assert changed[0] in check.select_tests("host", changed=changed)[0]


def test_a_tier_left_with_no_changed_test_to_run_keeps_its_own_suite() -> None:
    selected, details = check.select_tests("storage", changed=CHANGES["host test"])

    assert selected == check.SUITES["storage"]
    assert details["required_tiers"] == ["storage", "host"]


def test_a_changed_test_a_tier_that_refuses_child_processes_already_runs_is_selected() -> None:
    selected, details = check.select_tests("unit", changed=CHANGES["contract test"])

    assert CHANGES["contract test"][0] in selected
    assert details["required_tiers"] == ["unit"]


@pytest.mark.parametrize("tier", ["unit", "storage", "host", "release"])
def test_a_changed_test_no_suite_lists_is_selected_by_every_tier(tier, monkeypatch, tmp_path) -> None:
    unlisted = "tests/contract/test_not_in_a_suite_yet.py"
    (tmp_path / "tests" / "contract").mkdir(parents=True)
    (tmp_path / unlisted).write_text("def test_it():\n    pass\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(check, "ROOT", tmp_path)

    selected, _ = check.select_tests(tier, changed=[unlisted])

    assert unlisted in selected


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
    """Other tiers grow with their selection; integration's fixed budget
    already covers its whole real baseline, so it stays put."""
    small = check.pytest_watchdog_seconds("contract", 10)
    large = check.pytest_watchdog_seconds("contract", 60)
    assert large > small >= check.DEFAULT_WATCHDOG_SECONDS


def test_the_real_integration_selection_is_not_at_its_own_limit() -> None:
    """The budget for the real integration selection is not its own limit: the gate reports instead of timing out."""
    selected, _details = check.select_tests("integration", changed=[])
    budget = check.pytest_watchdog_seconds("integration", len(selected))
    # On the GitHub Windows runner the whole baseline needs tens of minutes: 600s
    # would end it a quarter of the way through, while it is still printing progress.
    assert budget >= check.INTEGRATION_WATCHDOG_SECONDS


def test_the_integration_budget_is_its_own_not_the_release_budget() -> None:
    """Integration carries a fixed budget sized to the Windows runner, exactly
    the way release carries 600s; the two must not collapse into one number."""
    assert check.pytest_watchdog_seconds("integration", 0) == check.INTEGRATION_WATCHDOG_SECONDS
    assert check.pytest_watchdog_seconds("integration", 10_000) == check.INTEGRATION_WATCHDOG_SECONDS
    assert check.INTEGRATION_WATCHDOG_SECONDS > check.RELEASE_WATCHDOG_SECONDS


def test_no_run_can_wait_longer_than_the_release_budget() -> None:
    assert check.pytest_watchdog_seconds("contract", 10_000) == check.RELEASE_WATCHDOG_SECONDS


def test_a_nonsense_count_falls_back_to_the_floor() -> None:
    for bad in (None, -1, "12", 1.5, True):
        assert check.pytest_watchdog_seconds("contract", bad) == check.DEFAULT_WATCHDOG_SECONDS
        assert check.pytest_watchdog_seconds("integration", bad) == (check.INTEGRATION_WATCHDOG_SECONDS)


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
    """A fresh checkout is refused early with the missing gate and a remedy, not failed on
    ``assert 'entry_point_missing' == 'host_config_missing'``, which names neither the cause nor the cure."""
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


# --- a contract file nothing selects protects nothing -------------------------

#: Contract files that deliberately stay out of the gate, each with the reason.
#: Anything else under tests/contract must be selected by some tier, or it is a
#: file that was written as a contract and is run by nothing.
UNGATED_CONTRACTS_BY_DESIGN: set[str] = set()


def test_every_contract_file_is_selected_by_some_tier():
    """A contract file no tier selects runs nowhere: its passing assertions protect nothing and its failures are
    not seen, even when a deliberate change to the contract should turn the file red."""
    import pathlib

    from check import SUITES, select_tests

    root = pathlib.Path(__file__).resolve().parents[2]
    on_disk = {"tests/contract/" + path.name for path in (root / "tests" / "contract").glob("test_*.py")}
    selected = set()
    for tier in SUITES:
        selected.update(select_tests(tier)[0])
    unselected = sorted(on_disk - selected - UNGATED_CONTRACTS_BY_DESIGN)
    assert unselected == [], (
        "these contract files are run by no tier; add them to CORE_RELEASE_CONTRACTS "
        "or name them in UNGATED_CONTRACTS_BY_DESIGN with the reason: %r" % unselected
    )


def test_the_exclusions_are_real_files():
    """An excuse for a file that no longer exists hides the next real gap."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    for relative in UNGATED_CONTRACTS_BY_DESIGN:
        assert (root / relative).is_file(), relative


def test_release_selects_everything_integration_selects():
    """``release`` is built from unit/contract/native/host/migration/packaging and
    not from SUITES["integration"], so a file added only to the integration tier
    runs in CI and not in the gate that decides whether something ships."""
    from check import select_tests

    integration = set(select_tests("integration")[0])
    release = set(select_tests("release")[0])
    contract_only_in_ci = sorted(path for path in integration - release if path.startswith("tests/contract/"))
    assert contract_only_in_ci == [], contract_only_in_ci
