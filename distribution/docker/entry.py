"""Start the registered remote servers under one container-wide store lease."""

from __future__ import annotations

import errno
import json
import os
import shlex
import sys
from pathlib import Path
from typing import IO

from ...adapters.clients.remote_server import RemoteServerConfig, load_server_config
from ...core.writer_lease import TruthWriterLease, try_lock_exclusive_nonblocking

DATA = Path("/data")
SECRET = Path("/run/secrets/runtime")
TEMPLATE = Path("/etc/scope-recall/supervisord.conf")


def acquire_container_lock(store: Path) -> IO[bytes]:
    """Lock a store until the returned handle is closed, or refuse a competing container."""
    store = Path(store)
    if not store.is_dir():
        raise ValueError("shared store is missing; initialize it with docker compose run --rm")
    handle = (store / ".container.lock").open("a+b")
    if not try_lock_exclusive_nonblocking(handle):
        handle.close()
        raise RuntimeError("This store is already in use by another container or local process")
    # Detect a writer already running outside this container before starting any services.
    # The separate lease stays free so the container's worker and servers can write.
    writer = TruthWriterLease(store, role="provider")
    if writer.acquire().get("status") != "acquired":
        handle.close()
        raise RuntimeError("This store is already in use by another container or local process")
    writer.release()
    return handle


def discover_entries(homes: Path) -> list[RemoteServerConfig]:
    """Read and validate remote registrations directly under the homes directory."""
    entries = []
    for path in sorted(Path(homes).glob("*/scope-recall/remote-server.json")):
        home = path.parent.parent
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            host = raw.get("host") if isinstance(raw, dict) else None
            if host not in {"codex", "claude-code", "workbuddy", "dsh"}:
                raise ValueError("invalid remote host")
            entries.append(load_server_config(home, host))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid remote entry at {home}: {exc}") from exc
    if not entries:
        raise ValueError("no remote entries; initialize and attach one with docker compose run --rm")
    return entries


def _argument(value: str) -> str:
    """Quote a Supervisor argument without allowing config interpolation or comments."""
    if any(char in value for char in "\r\n\x00;% "):
        raise ValueError("paths containing spaces, percent signs, semicolons or line breaks are unsupported")
    return shlex.quote(value)


def render_programs(entries: list[RemoteServerConfig], env_file: Path) -> str:
    """Render one restarting program for each validated remote entry."""
    lines = []
    for index, config in enumerate(entries):
        home = _argument(str(config.home))
        secret = _argument(str(env_file))
        lines.append(
            f"[program:remote-{index}]\n"
            f"command=/usr/local/bin/python -m scope_recall.adapters.codex.remote_server "
            f"serve --home {home} --host {config.host} --env-file {secret}\n"
            "autostart=true\nautorestart=true\nstartsecs=1\nstopasgroup=true\nkillasgroup=true\n"
            "stdout_logfile=/dev/fd/1\nstdout_logfile_maxbytes=0\n"
            "stderr_logfile=/dev/fd/2\nstderr_logfile_maxbytes=0\n"
        )
    return "\n".join(lines)


def preserve_lock_for_exec(fd: int) -> None:
    """Keep the lease through Supervisor's descriptor cleanup on a reload."""
    if fd in (3, 4):
        os.set_inheritable(fd, True)
        return
    if fd < 3:
        raise RuntimeError("a standard stream is unavailable; cannot preserve the container lease")
    # Supervisor retains descriptors 3 and 4 on reload. Never replace an
    # inherited descriptor: fail closed if neither slot is free.
    for target in (3, 4):
        try:
            os.fstat(target)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
            os.dup2(fd, target, inheritable=True)
            return
    raise RuntimeError("cannot preserve the container lease across supervisor reload")


def main() -> int:
    """Validate the mounted store and replace this process with foreground Supervisor."""
    from ...adapters.clients.config import load_shared_client

    try:
        store = DATA / "store"
        handle = acquire_container_lock(store)
        entries = discover_entries(DATA / "homes")
        for config in entries:
            client = load_shared_client(config.home, config.host)
            if client.data_directory.resolve() != store.resolve():
                raise ValueError(f"remote entry {config.home} points to another store")
        if not SECRET.is_file():
            raise ValueError("runtime credentials file is missing")
        runtime = DATA / ".runtime"
        runtime.mkdir(exist_ok=True)
        config_path = runtime / "supervisord.conf"
        config_path.write_text(
            TEMPLATE.read_text(encoding="utf-8") + "\n" + render_programs(entries, SECRET), encoding="utf-8"
        )
        preserve_lock_for_exec(handle.fileno())
        os.execvp("supervisord", ["supervisord", "-n", "-c", str(config_path)])
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"scope-recall container: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
