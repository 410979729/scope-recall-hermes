"""Real stdio service shutdown for every MCP host, without a host installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from scope_recall.adapters.shared_entries import attach_shared_record, client_entry_record

from tests.host.codex.test_shared_client import NOW, store


@pytest.mark.parametrize("host", ["codex", "claude-code", "workbuddy", "dsh"])
def test_mcp_exits_after_stdin_closes(store, tmp_path, host):
    root, _homes, _client, scope = store
    home = tmp_path / f"TEST-{host}-stdio"
    attach_shared_record(
        root,
        client_entry_record(
            host=host,
            home=home,
            entry_id="stdio-client",
            display_name="Synthetic stdio",
            attached_at=NOW,
            allowed_scope_ids=[scope],
            writable_scope_ids=[scope],
            capture_scope_id=scope,
        ),
        now=NOW,
    )
    command = [
        sys.executable,
        "-B",
        "-m",
        "scope_recall.adapters.clients.mcp_entry",
        "--home",
        str(home),
        "--host",
        host,
    ]
    # No ambient real home or model configuration is used; stdin is a real anonymous host pipe.
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        child.stdin.write(
            (
                json.dumps(
                    dict(
                        jsonrpc="2.0",
                        id=1,
                        method="initialize",
                        params=dict(
                            protocolVersion="2025-03-26",
                            capabilities={},
                            clientInfo=dict(name="synthetic", version="1"),
                        ),
                    )
                )
                + "\n"
            ).encode()
        )
        child.stdin.flush()
        assert child.stdout.readline(), child.stderr.read()
        began = time.monotonic()
        child.stdin.close()
        child.wait(timeout=5)
        assert time.monotonic() - began < 4
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()


@pytest.mark.parametrize("host", ["codex", "claude-code", "workbuddy", "dsh"])
def test_mcp_exits_when_parent_is_gone_even_with_stdin_open(store, tmp_path, host):
    import queue
    import threading

    from scope_recall.runtime.process_probe import probe_process

    root, _homes, _client, scope = store
    home = tmp_path / f"TEST-{host}-parent"
    attach_shared_record(
        root,
        client_entry_record(
            host=host,
            home=home,
            entry_id="parent-client",
            display_name="Synthetic parent",
            attached_at=NOW,
            allowed_scope_ids=[scope],
            writable_scope_ids=[scope],
            capture_scope_id=scope,
        ),
        now=NOW,
    )
    launcher = """
import subprocess,sys
child=subprocess.Popen(sys.argv[1:],stdin=sys.stdin,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
assert child.stdout.readline()
print(child.pid,flush=True)
"""
    command = [
        sys.executable,
        "-B",
        "-m",
        "scope_recall.adapters.clients.mcp_entry",
        "--home",
        str(home),
        "--host",
        host,
    ]
    parent = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", launcher, *command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pid = None
    try:
        parent.stdin.write(
            (
                json.dumps(
                    dict(
                        jsonrpc="2.0",
                        id=1,
                        method="initialize",
                        params=dict(
                            protocolVersion="2025-03-26",
                            capabilities={},
                            clientInfo=dict(name="synthetic", version="1"),
                        ),
                    )
                )
                + "\n"
            ).encode()
        )
        parent.stdin.flush()
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(parent.stdout.readline()), daemon=True).start()
        pid = int(ready.get(timeout=15))
        parent.wait(timeout=5)
        until = time.monotonic() + 4
        while probe_process(pid).running and time.monotonic() < until:
            time.sleep(0.05)
        assert not probe_process(pid).running
    finally:
        parent.stdin.close()
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if pid is not None and probe_process(pid).running:
            # This pid belongs only to the synthetic child whose parent just returned it.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
            else:
                import signal

                os.kill(pid, signal.SIGKILL)
        parent.stdout.close()
        parent.stderr.close()
