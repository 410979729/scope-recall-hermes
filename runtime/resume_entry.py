"""One external scheduler wake. No recursion, model call, or unlimited drain."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import re

from .scheduling import next_wake
from .worker_entry import _atomic_metadata, load_config
from .worker_launch import launch_worker


def control_path(config):
    return config.binding.data_directory / "runtime-autostart.json"


def read_control(config):
    path = control_path(config)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("autostart_control_invalid")
    control = json.loads(path.read_text(encoding="utf-8"))
    if control.get("installation_id") != config.binding.installation_id or type(control.get("enabled")) is not bool:
        raise ValueError("autostart_binding_invalid")
    expected = "ScopeRecall-"+hashlib.sha256(config.binding.installation_id.encode()).hexdigest()[:20]
    if control.get("task_name") != expected:
        raise ValueError("autostart_task_identity_invalid")
    return control


def credential_environment(config, env_file):
    """Read only the configured credential keys; never execute/interpolate dotenv."""
    names = {route.credential_env for route in (getattr(config.auxiliary, "embedding", None),
              getattr(config.auxiliary, "consolidation", None)) if route is not None}
    if not env_file or not names:
        return {}
    path = Path(env_file)
    if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 1048576:
        raise ValueError("autostart_environment_invalid")
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if match is None or match[1] not in names:
            continue
        value = match[2]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]
        if value and "\x00" not in value and len(value) <= 8192:
            result[match[1]] = value
    return result


def host_process_credential_environment(runtime_config_path, env_file):
    """Credential variables for a process the host launches with its own environment.

    The worker receives them through its autostart control file (``resume_once``);
    a Codex-launched MCP server or hook starts with Codex's environment, which carries
    none of the configured credential names, so it is given the same two paths and
    reads them under the same contract: only the names the trusted runtime config
    declares, never an interpolated dotenv.  Raises ``ValueError`` for an unusable
    env file and ``OSError``/``ValueError`` for an unreadable config; the caller
    decides whether a process may run without credentials.
    """
    config_path = Path(runtime_config_path)
    if not config_path.is_absolute():
        raise ValueError("runtime_config_path_not_absolute")
    return credential_environment(load_config(config_path), str(env_file))


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
    from .scheduling import SupervisorControl
    from ..file_lock import advisory_file_lock
    try:
        with advisory_file_lock(SupervisorControl(config).owner_lock, timeout_seconds=0):
            pass
    except TimeoutError:
        return dict(status="running", launched=False)
    environment = credential_environment(config, control.get("env_file"))
    worker = launcher(path, python_executable=control["python_executable"], detach_output=True, environment=environment)
    _atomic_metadata(config.binding.data_directory / "runtime-autostart-status.json",
                     dict(installation_id=config.binding.installation_id, last_wake_at=now.isoformat(), last_worker_pid=worker.pid))
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
