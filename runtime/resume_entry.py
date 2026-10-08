"""One external scheduler wake. No recursion, model call, or unlimited drain."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ..core.file_lock import advisory_file_lock
from .scheduling import SupervisorControl, next_wake, read_control
from .worker_entry import credential_environment, load_config, write_worker_metadata
from .worker_launch import launch_worker


def resume_once(config_path, *, launcher=launch_worker, now=None):
    path = Path(config_path).resolve()
    config = load_config(path)
    control = read_control(config)
    if control is None or not control["enabled"]:
        return dict(status="paused", launched=False)
    if Path(control["config_path"]).resolve() != path:
        raise ValueError("autostart_config_changed")
    if not config.supervisor_enabled:
        return dict(status="paused", launched=False)
    now = now or datetime.now(timezone.utc)
    plan = next_wake(config, now=now)
    if plan.due_at is None or datetime.fromisoformat(plan.due_at.replace("Z", "+00:00")) > now:
        return dict(status=plan.reason, launched=False, next_wake_at=plan.due_at)
    # Ownership is proved with the supervisor's OS lock, not a stale PID in a
    # status file. The watchdog coalesces if another wake wins this race.
    try:
        with advisory_file_lock(SupervisorControl(config).owner_lock, timeout_seconds=0):
            pass
    except TimeoutError:
        return dict(status="running", launched=False)
    environment = credential_environment(config, control.get("env_file"))
    worker = launcher(path, python_executable=control["python_executable"], detach_output=True, environment=environment)
    write_worker_metadata(
        config.binding.data_directory / "runtime-autostart-status.json",
        dict(installation_id=config.binding.installation_id, last_wake_at=now.isoformat(), last_worker_pid=worker.pid),
    )
    return dict(status="launched", launched=True, worker_pid=worker.pid)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = resume_once(args.config)
    except Exception as exc:
        print(json.dumps(dict(status="failed", error_type=type(exc).__name__)))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
