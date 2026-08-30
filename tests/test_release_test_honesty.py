"""Exact pytest accounting contracts for final release evidence."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_test_honesty.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_release_test_honesty_test",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(
    node_id: str,
    outcome: str,
    *,
    when: str = "call",
    reason: str = "",
    was_xfail: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        nodeid=node_id,
        outcome=outcome,
        when=when,
        longrepr=("file.py", 1, reason) if reason else "",
        wasxfail=was_xfail,
    )


def test_plugin_writes_exact_pass_skip_xfail_and_xpass_accounting(
    tmp_path: Path,
) -> None:
    module = _load_module()
    output = tmp_path / "honesty.json"
    plugin = module.ReleaseTestHonestyPlugin(
        output=output,
        source_commit="a" * 40,
        source_tree="b" * 40,
        timeout_overrides=[],
        first_failure_fixes=[],
    )
    plugin.pytest_sessionstart(SimpleNamespace())
    plugin.pytest_runtest_logreport(_report("tests/test_a.py::test_pass", "passed"))
    plugin.pytest_runtest_logreport(
        _report("tests/test_a.py::test_skip", "skipped", reason="platform unavailable")
    )
    plugin.pytest_runtest_logreport(
        _report("tests/test_a.py::test_xfail", "skipped", was_xfail=True)
    )
    plugin.pytest_runtest_logreport(
        _report("tests/test_a.py::test_xpass", "passed", was_xfail=True)
    )
    plugin.pytest_sessionfinish(SimpleNamespace(testscollected=4), 0)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["collected"] == 4
    assert payload["passed"] == 1
    assert payload["skipped"] == [
        {
            "node_id": "tests/test_a.py::test_skip",
            "reason": "platform unavailable",
        }
    ]
    assert payload["xfail"] == 1
    assert payload["xpass"] == 1
    assert payload["rerun_count"] == 0
    assert payload["source_commit"] == "a" * 40
    assert payload["source_tree"] == "b" * 40
    assert payload["first_failure_fixes_status"] == "not_provided"


def test_plugin_environment_is_explicit_and_rejects_malformed_arrays(
    tmp_path: Path,
) -> None:
    module = _load_module()
    environment = {
        module.OUTPUT_ENV: str(tmp_path / "honesty.json"),
        module.SOURCE_COMMIT_ENV: "a" * 40,
        module.SOURCE_TREE_ENV: "b" * 40,
        module.TIMEOUTS_ENV: "[]",
        module.FAILURE_FIXES_ENV: "[]",
    }

    plugin = module.build_plugin_from_environment(environment)
    assert plugin is not None
    assert plugin.source_commit == "a" * 40
    assert plugin.source_tree == "b" * 40
    assert plugin.first_failure_fixes_status == "declared"

    environment[module.TIMEOUTS_ENV] = "{}"
    with pytest.raises(RuntimeError, match="JSON array"):
        module.build_plugin_from_environment(environment)


def test_plugin_registers_with_real_pytest(tmp_path: Path) -> None:
    module = _load_module()
    test_file = tmp_path / "test_plugin_probe.py"
    test_file.write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    output = tmp_path / "honesty.json"
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                (str(ROOT), str(Path(pytest.__file__).resolve().parents[1]))
            ),
            module.OUTPUT_ENV: str(output),
            module.SOURCE_COMMIT_ENV: "a" * 40,
            module.SOURCE_TREE_ENV: "b" * 40,
            module.TIMEOUTS_ENV: "[]",
            module.FAILURE_FIXES_ENV: "[]",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "scripts.release_test_honesty",
            test_file.name,
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["collected"] == 1
    assert payload["passed"] == 1
    assert payload["failed"] == 0
    assert payload["errors"] == 0


def test_raw_skip_reasons_are_preserved_and_shareable_copy_is_redacted(tmp_path: Path) -> None:
    module = _load_module()
    windows_home = "\\".join(
        ("C:", "Users", "private-operator", "AppData", "Local", "Temp", "probe.db")
    )
    windows_repr_source = "\\\\".join(
        ("C:", "Users", "private-operator", "Temp", "source.db")
    )
    windows_repr_target = "\\\\".join(
        ("F:", "Agents", "runtime", "private-state", "target.db")
    )
    posix_home = "/" + "/".join(("home", "private-operator", "tmp", "probe.db"))
    posix_tmp = "/" + "/".join(("tmp", "private-operator", "probe.db"))
    plugin = module.ReleaseTestHonestyPlugin(
        output=tmp_path / "honesty.json",
        source_commit="a" * 40,
        source_tree="b" * 40,
        timeout_overrides=[],
        first_failure_fixes=[],
    )

    plugin.pytest_runtest_logreport(
        _report(
            "tests/test_a.py::test_windows",
            "skipped",
            reason=f"missing {windows_home}",
        )
    )
    plugin.pytest_runtest_logreport(
        _report(
            "tests/test_a.py::test_posix",
            "skipped",
            reason=f"missing {posix_home}",
        )
    )
    plugin.pytest_runtest_logreport(
        _report(
            "tests/test_a.py::test_windows_repr",
            "skipped",
            reason=(
                f"symlink '{windows_repr_source}' -> '{windows_repr_target}'"
            ),
        )
    )
    plugin.pytest_runtest_logreport(
        _report(
            "tests/test_a.py::test_posix_tmp",
            "skipped",
            reason=f"missing {posix_tmp}",
        )
    )

    raw_payload = plugin.payload(collected=4)
    raw_rendered = json.dumps(raw_payload)
    shareable_rendered = json.dumps(module.shareable_payload(raw_payload))
    raw_reasons = "\n".join(entry["reason"] for entry in raw_payload["skipped"])
    assert "private-operator" in raw_rendered
    assert windows_repr_target in raw_reasons
    assert "private-operator" not in shareable_rendered
    assert windows_repr_target not in shareable_rendered
    assert "<isolated-path>" in shareable_rendered


def test_plugin_counts_non_call_failure_as_error_and_rerun_honestly(
    tmp_path: Path,
) -> None:
    module = _load_module()
    plugin = module.ReleaseTestHonestyPlugin(
        output=tmp_path / "honesty.json",
        source_commit="a" * 40,
        source_tree="b" * 40,
        timeout_overrides=[],
        first_failure_fixes=[],
    )

    plugin.pytest_runtest_logreport(
        _report("tests/test_a.py::test_setup", "failed", when="setup")
    )
    plugin.pytest_runtest_logreport(
        _report("tests/test_a.py::test_retry", "rerun")
    )
    payload = plugin.payload(collected=2)

    assert payload["errors"] == 1
    assert payload["failed"] == 0
    assert payload["rerun_count"] == 1
