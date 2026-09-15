"""The autostart CLI fills in the interpreter and principal an operator would otherwise
have to spell out, and reports contract failures as JSON instead of a traceback.

Regression for 2026-09-15: ``autostart plan --config ...`` without ``--python`` died with
``TypeError: expected str, bytes or os.PathLike object, not NoneType`` inside pathlib."""
from __future__ import annotations

import json
import os
import sys

import pytest

from scope_recall.maintenance import autostart


def _run(monkeypatch, capsys, argv, plan_impl):
    monkeypatch.setattr(autostart, "plan", plan_impl)
    code = autostart.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_plan_defaults_to_this_interpreter_and_the_current_account(monkeypatch, capsys, tmp_path):
    seen = {}

    def fake_plan(config_path, python_executable, *, user_id, env_file=None):
        seen.update(config=config_path, python=python_executable, user_id=user_id, env_file=env_file)
        return {"task_name": "ScopeRecall-test", "xml": "<Task/>"}

    monkeypatch.setenv("USERNAME", "operator")
    code, out = _run(monkeypatch, capsys, ["plan", "--config", str(tmp_path / "runtime-config.json")], fake_plan)
    assert code == 0
    assert out["task_name"] == "ScopeRecall-test"
    assert seen["python"] == sys.executable
    assert seen["user_id"] == "operator"
    assert seen["env_file"] is None


def test_explicit_arguments_still_win(monkeypatch, capsys, tmp_path):
    seen = {}

    def fake_plan(config_path, python_executable, *, user_id, env_file=None):
        seen.update(python=python_executable, user_id=user_id, env_file=env_file)
        return {"task_name": "ScopeRecall-test"}

    code, _ = _run(monkeypatch, capsys, [
        "plan", "--config", str(tmp_path / "c.json"), "--python", r"C:\other\python.exe",
        "--user-id", "svc-account", "--env-file", str(tmp_path / ".env"),
    ], fake_plan)
    assert code == 0
    assert seen == {"python": r"C:\other\python.exe", "user_id": "svc-account", "env_file": str(tmp_path / ".env")}


@pytest.mark.parametrize("failure", ["autostart_user_required", "autostart_absolute_paths_required"])
def test_contract_failures_are_reported_as_json_not_tracebacks(monkeypatch, capsys, tmp_path, failure):
    def failing_plan(config_path, python_executable, *, user_id, env_file=None):
        raise ValueError(failure)

    code, out = _run(monkeypatch, capsys, ["plan", "--config", str(tmp_path / "c.json")], failing_plan)
    assert code == 2
    assert out == {"status": "error", "code": failure}


def test_no_account_in_environment_means_no_default_principal(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert autostart._current_user() is None
    assert os.environ.get("USERNAME") is None
