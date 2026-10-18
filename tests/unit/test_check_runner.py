from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import check  # noqa: E402


def _write_fixed_junit(command: list[str]) -> None:
    junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
    junit = Path(junit_arg.split("=", 1)[1])
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.write_text(
        "<testsuite tests='2' failures='0' errors='0' skipped='0'>"
        "<testcase classname='fake' name='one'/><testcase classname='fake' name='two'/>"
        "</testsuite>\n",
        encoding="utf-8",
    )


class ControlledDirectory:
    fail_cleanup = False

    def __init__(self, prefix: str, dir: Path) -> None:
        self.name = tempfile.mkdtemp(prefix=prefix, dir=dir)

    def cleanup(self) -> None:
        if self.fail_cleanup:
            raise RuntimeError("simulated cleanup failure")
        shutil.rmtree(self.name)


def _run_check(monkeypatch, tmp_path: Path, *, fail_cleanup: bool) -> tuple[int, dict, str]:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "pass_test.py").write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "v11_guard.py").write_text("", encoding="utf-8")

    def fake_run(command, **_kwargs):
        # The P17 runner also shells out to git for the source manifest
        # (binary stdout); only the pytest invocation carries --junitxml.
        if "ls-files" in command:
            return check.subprocess.CompletedProcess(command, 0, b"tests/unit/pass_test.py\0", b"")
        if "hash-object" in command:
            blob = "0" * 40
            return check.subprocess.CompletedProcess(command, 0, (blob + "\n").encode("ascii"), b"")
        _write_fixed_junit(command)
        return check.subprocess.CompletedProcess(command, 0, "fixed pytest output\n", "")

    def fake_check_output(command, **_kwargs):
        if command[:2] == ["git", "ls-files"]:
            return "tests/unit/pass_test.py\n"
        return "TEST-HEAD\n"

    # Through monkeypatch: a plain assignment outlived the test and failed the
    # next one's cleanup.
    monkeypatch.setattr(ControlledDirectory, "fail_cleanup", fail_cleanup)
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check, "SUITES", {"unit": ["tests/unit/pass_test.py"]})
    # The nested runner must keep its owned directory inside this pytest
    # fixture; its normal Windows temp parent is outside the outer guard.
    monkeypatch.setattr(
        check,
        "TestDirectory",
        lambda *, prefix, dir: ControlledDirectory(prefix=prefix, dir=tmp_path),
    )
    monkeypatch.setattr(check.subprocess, "run", fake_run)
    monkeypatch.setattr(check.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(sys, "argv", ["check.py", "--tier", "unit", "--task", "P07"])

    exit_code = check.main()
    receipts = sorted(
        path for path in (tmp_path / "verification" / "P07").glob("*.json") if not path.name.endswith("-inputs.json")
    )
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    log = (tmp_path / receipt["log"]).read_text(encoding="utf-8")
    return exit_code, receipt, log


def test_cleanup_failure_is_recorded_and_nonzero(monkeypatch, tmp_path: Path) -> None:
    exit_code, receipt, log = _run_check(monkeypatch, tmp_path, fail_cleanup=True)

    assert exit_code != 0
    assert receipt["pytest_exit_code"] == 0
    assert receipt["wrapper_exit_code"] == 0
    assert receipt["cleanup_exit_code"] != 0
    assert receipt["overall_exit_code"] != 0
    assert receipt["exit_code"] == receipt["overall_exit_code"]
    assert receipt["cleanup"]["state"] == "failed"
    assert receipt["cleanup"]["error"] == {
        "type": "RuntimeError",
        "reason": "simulated cleanup failure",
    }
    assert "pytest_completed_waiting_cleanup" in receipt["transition_history"]
    assert "cleanup_failed" in receipt["transition_history"]
    assert "RuntimeError" in log
    assert "simulated cleanup failure" in log


def test_watchdog_seconds_are_bounded_and_release_is_600() -> None:
    assert check.DEFAULT_WATCHDOG_SECONDS == 180
    assert check.RELEASE_WATCHDOG_SECONDS == 600
    assert check.pytest_watchdog_seconds("unit") == 180
    assert check.pytest_watchdog_seconds("contract") == 180
    assert check.pytest_watchdog_seconds("packaging") == 180
    assert check.pytest_watchdog_seconds("release") == 600
    # The GitHub Windows runner needs tens of minutes for the 118-file
    # integration baseline; the tier's floor must exceed the release budget.
    assert check.pytest_watchdog_seconds("integration", 118) == check.INTEGRATION_WATCHDOG_SECONDS
    assert check.INTEGRATION_WATCHDOG_SECONDS > check.RELEASE_WATCHDOG_SECONDS
    assert check.pytest_watchdog_seconds("integration", 0) == check.INTEGRATION_WATCHDOG_SECONDS


def test_the_retrieval_tier_s_watchdog_carries_the_recall_bench_s_own_allowance() -> None:
    """The recall bench builds a whole synthetic store before its tests run, more work on a slow runner than the
    20s a file its tier gives: its allowance comes on top of them, after the release budget has capped them, so a
    tier the cap already holds still gets all of it, and the sum is still a bound."""
    bench = "tests/recall_bench/test_recall_bench.py"
    selected = list(check.SUITES["retrieval"])
    rest = [path for path in selected if path != bench]
    assert bench in selected
    assert check.HEAVY_FILE_SECONDS[bench] == 600
    assert check.pytest_watchdog_seconds("retrieval", len(rest), rest) == 180 + 20 * len(rest)
    assert check.pytest_watchdog_seconds("retrieval", len(selected), selected) == 180 + 20 * len(selected) + 600
    assert check.pytest_watchdog_seconds("retrieval", 1, [bench]) == 800
    assert check.pytest_watchdog_seconds("retrieval", 40, [bench]) == check.RELEASE_WATCHDOG_SECONDS + 600


def test_release_wrapper_records_actual_600s_watchdog(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "pass_test.py").write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "v11_guard.py").write_text("", encoding="utf-8")

    def fake_run(command, **kwargs):
        if "ls-files" in command:
            return check.subprocess.CompletedProcess(command, 0, b"tests/unit/pass_test.py\0", b"")
        if "hash-object" in command:
            return check.subprocess.CompletedProcess(command, 0, (b"0" * 40) + b"\n", b"")
        if any(isinstance(item, str) and item.startswith("--junitxml=") for item in command):
            captured["timeout"] = kwargs.get("timeout")
            _write_fixed_junit(command)
            return check.subprocess.CompletedProcess(command, 0, "fixed pytest output\n", "")
        return check.subprocess.CompletedProcess(command, 0, "", "")

    def fake_check_output(command, **_kwargs):
        if command[:2] == ["git", "ls-files"]:
            return "tests/unit/pass_test.py\n"
        return "TEST-HEAD\n"

    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(
        check, "select_tests", lambda tier, changed=None: (["tests/unit/pass_test.py"], {"mode": "fixture"})
    )
    monkeypatch.setattr(
        check,
        "TestDirectory",
        lambda *, prefix, dir: ControlledDirectory(prefix=prefix, dir=tmp_path),
    )
    monkeypatch.setattr(check.subprocess, "run", fake_run)
    monkeypatch.setattr(check.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(sys, "argv", ["check.py", "--tier", "release", "--task", "P07"])

    exit_code = check.main()
    receipts = sorted(
        path for path in (tmp_path / "verification" / "P07").glob("*.json") if not path.name.endswith("-inputs.json")
    )
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert captured["timeout"] == 600
    assert receipt["watchdog_seconds"] == 600
    assert receipt["pytest_exit_code"] == 0
    assert receipt["missing_gates"] == [], "no model gate since 3.4.0rc6"
    assert exit_code == 0


def test_pytest_and_cleanup_success_is_zero(monkeypatch, tmp_path: Path) -> None:
    exit_code, receipt, log = _run_check(monkeypatch, tmp_path, fail_cleanup=False)

    assert exit_code == 0
    assert receipt["pytest_exit_code"] == 0
    assert receipt["wrapper_exit_code"] == 0
    assert receipt["cleanup_exit_code"] == 0
    assert receipt["overall_exit_code"] == 0
    assert receipt["cleanup"] == {"state": "succeeded", "exit_code": 0, "error": None}
    assert "pytest_completed_waiting_cleanup" in receipt["transition_history"]
    assert "cleanup_succeeded" in receipt["transition_history"]
    assert "cleanup_failed" not in log


# --- one table says what each tier's runs may do ------------------------------


def test_the_process_tiers_are_the_tiers_the_table_lists() -> None:
    assert check.PROCESS_TIERS == frozenset(check.TIER_ALLOWS)
    assert set(check.TIER_ALLOWS) <= set(check.SUITES)


@pytest.mark.parametrize("tier", sorted(check.SUITES))
def test_a_tier_gets_the_helpers_the_table_lets_it_use(tier) -> None:
    allows = check.TIER_ALLOWS.get(tier, frozenset())

    assert (check.packaging_helper_env(tier) != {}) <= ("build" in allows)
    assert (check.node_helper_env(tier) != {}) <= ("node" in allows)


def test_a_declared_uv_goes_into_the_runs_that_drive_it(monkeypatch, tmp_path: Path) -> None:
    """The packaging tests take the uv ``SCOPE_RECALL_UV`` names before they search ``PATH``, and the run authorizes
    the directory of that uv; a run handed only ``PATH`` would find another uv first, outside that directory."""
    uv = tmp_path / "declared" / "uv"
    uv.parent.mkdir()
    uv.write_bytes(b"")
    monkeypatch.setenv("SCOPE_RECALL_UV", str(uv))

    for tier in ("packaging", "release"):
        env = check.packaging_helper_env(tier)
        assert env["SCOPE_RECALL_UV"] == str(uv)
        assert env["SCOPE_RECALL_TEST_PACKAGING_HELPER_ROOTS"] == str(uv.parent.resolve())
    for tier in ("unit", "native", "host"):
        assert check.packaging_helper_env(tier) == {}
    run = tmp_path / "run"
    run.mkdir()
    assert check._test_environment("packaging", run)["SCOPE_RECALL_UV"] == str(uv)


def test_a_run_without_a_declared_uv_is_handed_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SCOPE_RECALL_UV", raising=False)
    run = tmp_path / "run"
    run.mkdir()

    assert "SCOPE_RECALL_UV" not in check.packaging_helper_env("packaging")
    assert "SCOPE_RECALL_UV" not in check._test_environment("packaging", run)


def test_the_guard_takes_what_a_tier_may_do_from_the_table() -> None:
    """A second copy of the table in the guard would let the runner select what the guard then refuses."""
    tree = ast.parse((ROOT / "tests" / "v11_guard.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    assert "tier_allows" in imported
    assert not strings & set(check.TIER_ALLOWS)
