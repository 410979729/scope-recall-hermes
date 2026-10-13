"""The autostart CLI fills in the interpreter and principal an operator would otherwise
have to spell out, and reports contract failures as JSON instead of a traceback.

Regression for 2026-09-15: ``autostart plan --config ...`` without ``--python`` died with
``TypeError: expected str, bytes or os.PathLike object, not NoneType`` inside pathlib."""

from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
import sys

import pytest
from scope_recall.maintenance import autostart, windows_wake


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

    code, _ = _run(
        monkeypatch,
        capsys,
        [
            "plan",
            "--config",
            str(tmp_path / "c.json"),
            "--python",
            r"C:\other\python.exe",
            "--user-id",
            "svc-account",
            "--env-file",
            str(tmp_path / ".env"),
        ],
        fake_plan,
    )
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


def test_outside_windows_the_plan_is_a_timer_for_the_operator_and_enable_writes_only_its_control(tmp_path, monkeypatch):
    """Outside Windows the plan gives the wake as a systemd user timer and a cron line, and ``enable`` writes the
    control file the wake reads, credentials file included; the timer is the operator's to install."""
    from datetime import timedelta
    from pathlib import Path
    from types import SimpleNamespace

    from scope_recall.runtime.resume_entry import resume_once
    from scope_recall.runtime.scheduling import read_control
    from test_finite_supervisor import NOW, fixture, queue

    core, config, written = fixture(tmp_path)
    path = config.binding.data_directory / "runtime-config.json"
    path.write_bytes(written.read_bytes())
    monkeypatch.setattr(autostart, "_windows", lambda: False)
    monkeypatch.setattr(autostart.subprocess, "run", lambda *args, **kwargs: pytest.fail("schtasks was called"))

    planned = autostart.plan(path, Path(sys.executable), user_id=None)
    assert planned["registration"] == "operator_timer" and "xml" not in planned
    assert planned["wake_command"] == [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "scope_recall.runtime.resume_entry",
        "--config",
        str(path.resolve()),
    ]
    assert "ExecStart=" in planned["systemd_service"] and "OnUnitActiveSec=5min" in planned["systemd_timer"]
    # The wake exits as soon as it has launched a detached worker; the unit's default kill would end that worker.
    assert "\nKillMode=process\n" in planned["systemd_service"]
    # ``systemctl --user enable --now`` needs the timer to name its target.
    assert "[Install]\nWantedBy=timers.target" in planned["systemd_timer"]
    assert planned["cron"].startswith(f"*/5 * * * * {shlex.quote(sys.executable)} -I -B -m ")
    assert read_control(config) is None, "a plan changes nothing"

    env = tmp_path / "TEST-embedding.env"
    env.write_text("", encoding="utf-8")
    planned = autostart.plan(path, Path(sys.executable), user_id=None, env_file=env)
    enabled = autostart.apply(planned)
    control = read_control(config)
    assert control["enabled"] is True and control["registration"] == "operator_timer"
    assert control["env_file"] == str(env.resolve()), "the wake launches the worker with these credentials"
    assert not set(control) & {"wake_command", "systemd_service", "systemd_timer", "cron"}
    assert enabled["systemd_timer"] == planned["systemd_timer"] and "registers nothing" in enabled["next_step"]

    # The command the timer runs launches a worker when work is due, as the scheduled task's does.
    launched = []
    queue(core, config, due=NOW)
    assert resume_once(
        path,
        launcher=lambda *args, **kwargs: launched.append(args) or SimpleNamespace(pid=99),
        now=NOW + timedelta(minutes=1),
    )["launched"]
    assert autostart.disable(path)["status"] == "paused" and read_control(config)["enabled"] is False
    assert (
        resume_once(
            path, launcher=lambda *args, **kwargs: pytest.fail("launched while paused"), now=NOW + timedelta(minutes=2)
        )["status"]
        == "paused"
    )
    removed = autostart.disable(path, remove=True)
    assert removed["status"] == "removed" and "timer" in removed["next_step"]
    assert read_control(config)["registration_state"] == "removed"
    assert len(launched) == 1


def test_a_percent_sign_in_a_path_is_escaped_for_systemd_and_cron():
    """``%`` starts a specifier in a unit file and a new line of input in a crontab."""
    from pathlib import Path

    wake = autostart._posix_wake("ScopeRecall-TEST", Path("/srv/50%/runtime-config.json"), Path("/usr/bin/python3"))
    assert "50%%" in wake["systemd_service"] and "50%" not in wake["systemd_service"].replace("50%%", "")
    assert "50\\%" in wake["cron"] and "50%" not in wake["cron"].replace("50\\%", "")
    assert wake["wake_command"][-1].endswith("runtime-config.json") and "%%" not in wake["wake_command"][-1]


def test_a_dollar_or_a_backslash_in_a_path_reaches_systemd_as_written_and_a_line_break_is_refused():
    """In ``ExecStart`` a backslash is an escape everywhere and ``$`` a variable in the arguments, not in the program's
    path; no line break can be written into a unit or a crontab.  A oneshot without a start timeout would hold its
    timer for good."""
    from pathlib import PurePosixPath

    wake = autostart._posix_wake(
        "ScopeRecall-TEST", PurePosixPath("/srv/a$b\\c\\/runtime-config.json"), PurePosixPath("/opt/v$1\\x/bin/python3")
    )
    executed = next(line for line in wake["systemd_service"].splitlines() if line.startswith("ExecStart="))
    assert executed.startswith("ExecStart='/opt/v$1\\\\x/bin/python3' -I -B -m ")
    assert executed.endswith(" --config '/srv/a$$b\\\\c\\\\/runtime-config.json'")
    assert "'/opt/v$1\\x/bin/python3'" in wake["cron"] and "'/srv/a$b\\c\\/runtime-config.json'" in wake["cron"]
    assert "\nTimeoutStartSec=120\n" in wake["systemd_service"]
    for character in "\n\r\x00":
        with pytest.raises(ValueError, match="autostart_path_unsupported"):
            autostart._posix_wake(
                "ScopeRecall-TEST",
                PurePosixPath(f"/srv/a{character}b/runtime-config.json"),
                PurePosixPath("/usr/bin/python3"),
            )


def test_the_wake_runs_in_no_working_directory():
    """Its paths are absolute and the worker it launches sets its own, so no data directory is written as a unit's
    ``WorkingDirectory``, where a backslash ending it would continue the line into ``ExecStart`` and a space ending it
    would be lost.  A oneshot started at boot and every 5 minutes after; the wake is quoted as a shell and systemd
    both read it."""
    from pathlib import PurePosixPath

    wake = autostart._posix_wake(
        "ScopeRecall-TEST", PurePosixPath("/srv/TEST /runtime-config.json"), PurePosixPath("/usr/bin/python3")
    )
    assert wake["systemd_service"] == (
        "[Unit]\nDescription=Scope Recall wake (ScopeRecall-TEST)\n\n[Service]\nType=oneshot\nKillMode=process\n"
        "TimeoutStartSec=120\nExecStart=/usr/bin/python3 -I -B -m scope_recall.runtime.resume_entry "
        "--config '/srv/TEST /runtime-config.json'\n"
    )
    assert wake["systemd_timer"] == (
        "[Unit]\nDescription=Scope Recall wake every 5 minutes (ScopeRecall-TEST)\n\n[Timer]\nOnBootSec=1min\n"
        "OnUnitActiveSec=5min\n\n[Install]\nWantedBy=timers.target\n"
    )
    assert wake["cron"] == (
        "*/5 * * * * /usr/bin/python3 -I -B -m scope_recall.runtime.resume_entry "
        "--config '/srv/TEST /runtime-config.json' >/dev/null 2>&1"
    )


def test_cron_mails_nothing_and_a_backslash_before_a_percent_sign_is_refused():
    from pathlib import PurePosixPath

    wake = autostart._posix_wake(
        "ScopeRecall-TEST", PurePosixPath("/srv/runtime-config.json"), PurePosixPath("/usr/bin/python3")
    )
    assert wake["cron"].endswith(" >/dev/null 2>&1")
    with pytest.raises(ValueError, match="autostart_path_unsupported"):
        autostart._posix_wake(
            "ScopeRecall-TEST", PurePosixPath("/srv/a\\%b/runtime-config.json"), PurePosixPath("/usr/bin/python3")
        )


def _pe(path, subsystem):
    """Minimal PE header fixture: only subsystem selection is under test."""
    data = bytearray(160)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 88, 0x20B)
    struct.pack_into("<H", data, 156, subsystem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_gui_selection_checks_the_binary_not_the_pythonw_name(tmp_path):
    gui = _pe(tmp_path / "pythonw.exe", 3)
    assert not windows_wake.is_gui_executable(gui)
    _pe(gui, 2)
    assert windows_wake.is_gui_executable(gui)
    gui.write_bytes(b"MZ")
    assert not windows_wake.is_gui_executable(gui)
    assert not windows_wake.is_gui_executable(tmp_path / "missing.exe")


def test_a_real_gui_venv_launcher_needs_no_extra_process(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    gui = _pe(python.with_name("pythonw.exe"), 2)
    monkeypatch.setattr(windows_wake.subprocess, "run", lambda *a, **k: pytest.fail("unnecessary probe"))
    command = windows_wake.wake_command(python, tmp_path / "config.json")
    assert command == [
        str(gui),
        "-I",
        "-B",
        "-m",
        "scope_recall.runtime.resume_entry",
        "--config",
        str(tmp_path / "config.json"),
    ]


def test_a_console_redirector_is_hidden_without_losing_the_selected_venv(tmp_path, monkeypatch):
    python = tmp_path / "venv space" / "python.exe"
    _pe(python.with_name("pythonw.exe"), 3)
    base = tmp_path / "base" / "python.exe"
    gui = _pe(base.with_name("pythonw.exe"), 2)
    calls = []

    def probe(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, json.dumps(str(base)))

    monkeypatch.setattr(windows_wake.subprocess, "run", probe)
    config = tmp_path / "配置 space & name" / "runtime-config.json"
    command = windows_wake.wake_command(python, config)
    assert command[:5] == [str(gui), "-I", "-B", "-c", windows_wake._HIDDEN_WAKE]
    assert command[5:] == [str(python), "-I", "-B", "-m", "scope_recall.runtime.resume_entry", "--config", str(config)]
    assert calls[0][0][0] == str(python)
    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "CREATE_NO_WINDOW" in command[4] and "timeout=50" in command[4]


@pytest.mark.parametrize("failure", ["missing_gui", "console_base", "bad_probe", "probe_failed"])
def test_no_gui_interpreter_fails_closed_instead_of_registering_a_flashing_task(tmp_path, monkeypatch, failure):
    python = tmp_path / "venv" / "python.exe"
    _pe(python.with_name("pythonw.exe"), 3)
    base = tmp_path / "base" / "python.exe"
    if failure == "console_base":
        _pe(base.with_name("pythonw.exe"), 3)

    def probe(command, **options):
        if failure == "probe_failed":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "{}" if failure == "bad_probe" else json.dumps(str(base)))

    monkeypatch.setattr(windows_wake.subprocess, "run", probe)
    with pytest.raises(ValueError, match="^autostart_gui_interpreter_unavailable$"):
        windows_wake.wake_command(python, tmp_path / "config.json")
