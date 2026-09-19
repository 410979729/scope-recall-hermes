"""Reviewable Windows logon/periodic wake registration for one bound runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from ..runtime.resume_entry import control_path, read_control
from ..runtime.worker_entry import _atomic_metadata, load_config


def plan(config_path, python_executable, *, user_id, env_file=None):
    config_path, python = Path(config_path), Path(python_executable)
    if not config_path.is_absolute() or not python.is_absolute() or not python.is_file():
        raise ValueError("autostart_absolute_paths_required")
    config = load_config(config_path)
    if config_path.resolve().parent != config.binding.data_directory.resolve():
        raise ValueError("autostart_config_outside_binding")
    from ..core.storage import SQLiteStorage
    with SQLiteStorage(config.binding).read(config.context()) as tx:
        tx.status()
    if env_file and (not Path(env_file).is_absolute() or not Path(env_file).is_file()):
        raise ValueError("autostart_environment_missing")
    if not user_id or any(c in user_id for c in '\r\n\x00'):
        raise ValueError("autostart_user_required")
    name = "ScopeRecall-" + hashlib.sha256(config.binding.installation_id.encode()).hexdigest()[:20]
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def node(parent, tag, text=None, **attrs):
        elem = ET.SubElement(parent, "{"+namespace+"}"+tag, attrs)
        elem.text = text
        return elem
    task = ET.Element("{"+namespace+"}Task", version="1.2")
    triggers = node(task, "Triggers")
    logon = node(triggers, "LogonTrigger")
    node(logon, "Enabled", "true")
    node(logon, "UserId", user_id)
    timer = node(triggers, "TimeTrigger")
    repeat = node(timer, "Repetition")
    node(repeat, "Interval", "PT5M")
    node(repeat, "StopAtDurationEnd", "false")
    node(timer, "StartBoundary", (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(timespec="seconds"))
    node(timer, "Enabled", "true")
    principal = node(node(task, "Principals"), "Principal", id="Author")
    node(principal, "UserId", user_id)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    for key, value in (("MultipleInstancesPolicy","IgnoreNew"), ("DisallowStartIfOnBatteries","false"),
                       ("StopIfGoingOnBatteries","false"), ("StartWhenAvailable","true"),
                       ("Enabled","true"), ("Hidden","true"), ("ExecutionTimeLimit","PT1M")):
        node(settings, key, value)
    action = node(node(task, "Actions", Context="Author"), "Exec")
    # pythonw plus hidden task avoids console windows after logon.
    pythonw = python.with_name("pythonw.exe") if os.name == "nt" else python
    executable = pythonw if pythonw.is_file() else python
    node(action, "Command", str(executable))
    node(action, "Arguments", subprocess.list2cmdline(["-I", "-B", "-m", "scope_recall.runtime.resume_entry", "--config", str(config_path.resolve())]))
    node(action, "WorkingDirectory", str(config.binding.data_directory))
    return dict(installation_id=config.binding.installation_id, enabled=True, task_name=name,
                # The interpreter is recorded as given: the task must start the
                # venv launcher, not the base interpreter its symlink points at.
                config_path=str(config_path.resolve()), python_executable=str(python),
                env_file=str(Path(env_file).resolve()) if env_file else None,
                trigger="user_logon_and_every_5_minutes", xml=ET.tostring(task, encoding="unicode"))


def apply(prepared):
    if os.name != "nt":
        raise ValueError("autostart_windows_only")
    config = load_config(prepared["config_path"])
    previous = read_control(config)
    found = subprocess.run(["schtasks.exe", "/Query", "/TN", prepared["task_name"], "/XML"], capture_output=True, timeout=15)
    if found.returncode == 0 and (previous is None or previous.get("task_name") != prepared["task_name"]):
        raise ValueError("autostart_task_ownership_unverified")
    # Disabled control prevents a trigger racing registration from doing work.
    control = {k:v for k,v in prepared.items() if k != "xml"}
    _atomic_metadata(control_path(config), dict(control, enabled=False))
    with tempfile.TemporaryDirectory(prefix="scope-recall-task-") as temp:
        path = Path(temp)/"task.xml"
        path.write_text(prepared["xml"], encoding="utf-16")
        result = subprocess.run(["schtasks.exe", "/Create", "/TN", prepared["task_name"], "/XML", str(path), "/F"], capture_output=True, timeout=15)
    if result.returncode:
        if previous is not None:
            _atomic_metadata(control_path(config), previous)
        raise ValueError("autostart_registration_failed")
    _atomic_metadata(control_path(config), control)
    return control


def disable(config_path, *, remove=False):
    config = load_config(config_path)
    control = read_control(config)
    if control is None:
        return dict(status="not_registered")
    if remove and control.get("registration_state") == "removed":
        return dict(status="removed", task_name=control["task_name"])
    control["enabled"] = False
    _atomic_metadata(control_path(config), control)
    args = ["/Delete", "/TN", control["task_name"], "/F"] if remove else ["/Change", "/TN", control["task_name"], "/DISABLE"]
    result = subprocess.run(["schtasks.exe", *args], capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError("autostart_unregister_failed")
    if remove:
        control["registration_state"] = "removed"
        _atomic_metadata(control_path(config), control)
    return dict(status="removed" if remove else "paused", task_name=control["task_name"])


def _current_user() -> str | None:
    """The interactive account the operator is running as, or None when unknown."""
    return os.environ.get("USERNAME") or os.environ.get("USER") or None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "enable", "pause", "remove"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter the task runs; defaults to the one running this command")
    parser.add_argument("--user-id", default=_current_user(),
                        help="task principal; defaults to the current account")
    parser.add_argument("--env-file")
    args = parser.parse_args(argv)
    try:
        if args.command in {"pause", "remove"}:
            result = disable(args.config, remove=args.command == "remove")
        else:
            result = plan(args.config, args.python, user_id=args.user_id, env_file=args.env_file)
            if args.command == "enable":
                result = apply(result)
    except ValueError as exc:
        # Contract failures are reported like the rest of the maintenance CLI: one JSON
        # object with the failure code, exit 2, no traceback for a missing argument.
        print(json.dumps({"status": "error", "code": str(exc)}, ensure_ascii=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
