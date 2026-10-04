"""A client's resident prompt recall server (``adapters/codex/resident_entry``, ``local_endpoint.ensure_resident``).

WorkBuddy starts the entry's MCP server with each conversation's agent process, and a prompt that started one met a
server still opening its vector store: a cold server answered with its vector search 12.7 s after its start, past the
prompt hook's 6 s.  A resident server outlives those processes, and hooks ask it first.  Sources are synthetic.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from scope_recall.adapters.codex import local_endpoint, resident_entry
from scope_recall.adapters.hermes.installation import (
    attach_shared_entry,
    attach_shared_record,
    build_installation_manifest,
    client_entry_record,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)

NOW = "2026-10-03T20:00:00Z"
AGENT = "TEST-agent"


@pytest.fixture
def entry(tmp_path, monkeypatch):
    """A shared store with a Hermes entry and a WorkBuddy entry; no LanceDB helper process is started."""
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    root = tmp_path / "TEST-shared"
    write_shared_payload(root, new_shared_payload(root, agent_id=AGENT))
    hermes = tmp_path / "TEST-tianshu-home"
    hermes.mkdir()
    attach_shared_entry(root, build_installation_manifest(hermes, agent_id=AGENT, user_id="TEST-owner",
                                                          agent_workspace="TEST-workspace"),
                        entry_id="tianshu", display_name="天枢", now=NOW)
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    home = tmp_path / "TEST-workbuddy-home"
    attach_shared_record(root, client_entry_record(
        host="workbuddy", home=home, entry_id="workbuddy", display_name="WorkBuddy", attached_at=NOW,
        allowed_scope_ids=owner["allowed_scope_ids"], writable_scope_ids=owner["writable_scope_ids"],
        capture_scope_id=owner["capture_scope_id"]), now=NOW)
    return home


def _runtime_config(home, **fields):
    path = home / "scope-recall" / "runtime-config.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _name(home, *, pid, port, resident, host="workbuddy", start="TEST-start", version=None, age=0.0):
    """A server's name in the entry's folder, as ``HookEndpoint._advertise`` writes it."""
    from scope_recall._version import __version__

    folder = local_endpoint.endpoints(home)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pid}.json"
    path.write_text(json.dumps({"host": host, "port": port, "token": "TEST-token", "pid": pid, "start": start,
                                "version": version or __version__, "resident": resident}), encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


class _Running:
    running = True
    start_token = "TEST-start"


def test_a_hook_asks_the_resident_server_before_a_newer_one_the_client_started(entry, monkeypatch):
    """A server the client just started with a conversation may still be opening its vector store; the resident one
    is warm.  Newest first otherwise, as before."""
    from scope_recall.runtime import process_probe

    _name(entry, pid=41, port=4101, resident=True, age=300)
    _name(entry, pid=42, port=4102, resident=False, age=1)
    monkeypatch.setattr(process_probe, "probe_process", lambda pid: _Running())
    asked = []
    monkeypatch.setattr(local_endpoint.Recaller, "_exchange",
                        lambda self, port, token, request, until: (asked.append(port), ("busy", None))[1])
    recaller = local_endpoint.Recaller(entry, "workbuddy")
    assert recaller({"hook_event_name": "UserPromptSubmit"}, (), (), 5.0) is None
    assert asked == [4101, 4102]


def test_the_entry_s_runtime_config_names_its_resident_minutes_else_the_client_s_default(entry, tmp_path):
    assert local_endpoint.resident_minutes(entry, "workbuddy") == 0, "an entry with no runtime config keeps none"
    _runtime_config(entry, hook_processing_seconds=5.5)
    assert local_endpoint.resident_minutes(entry, "workbuddy") == 120
    _runtime_config(entry, resident_recall_minutes=30)
    assert local_endpoint.resident_minutes(entry, "workbuddy") == 30
    _runtime_config(entry, resident_recall_minutes=0)
    assert local_endpoint.resident_minutes(entry, "workbuddy") == 0
    for bad in (-1, 1441, 1.5, True, "60"):
        _runtime_config(entry, resident_recall_minutes=bad)
        assert local_endpoint.resident_minutes(entry, "workbuddy") == 0, bad


def test_the_runtime_config_bounds_resident_minutes():
    from scope_recall.runtime.instance import RESIDENT_RECALL_MINUTES_BOUNDS

    assert RESIDENT_RECALL_MINUTES_BOUNDS == (0, 1440)
    assert local_endpoint.RESIDENT_DEFAULT_MINUTES == {"workbuddy": 120}


def test_a_client_starts_a_resident_server_once_and_not_while_one_runs(entry, monkeypatch, tmp_path):
    from scope_recall.runtime import process_probe

    started = []
    monkeypatch.setattr(local_endpoint, "_start_apart", lambda command, cwd: (started.append(command), True)[1])
    env_file = tmp_path / "TEST.env"
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=0) == "off"
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120, env_file=env_file) == "started"
    assert started == [[sys.executable, "-I", "-B", "-m", "scope_recall.adapters.codex.resident_entry",
                        "--home", str(entry), "--host", "workbuddy", "--env-file", str(env_file)]]
    # The next prompt's hook, while that one still starts: no second start.
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120) == "recent"
    assert len(started) == 1
    _name(entry, pid=43, port=4103, resident=True)
    monkeypatch.setattr(process_probe, "probe_process", lambda pid: _Running())
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120) == "running"
    assert len(started) == 1


def test_a_resident_server_of_another_version_or_client_is_not_this_one(entry, monkeypatch):
    from scope_recall.runtime import process_probe

    monkeypatch.setattr(process_probe, "probe_process", lambda pid: _Running())
    _name(entry, pid=44, port=4104, resident=True, version="0.0.1")
    _name(entry, pid=45, port=4105, resident=True, host="codex")
    _name(entry, pid=46, port=4106, resident=False)
    assert not local_endpoint.resident_running(entry, "workbuddy")
    assert [int(info["pid"]) for _path, info in local_endpoint._residents(entry, "workbuddy", any_version=True)] == [44]


@pytest.mark.skipif(os.name != "nt", reason="job objects are Windows'")
def test_a_resident_server_breaks_away_from_the_client_s_job_where_it_may(monkeypatch, tmp_path):
    """WorkBuddy ends a conversation's processes; started from them, the server must outlive them.  A job that does
    not allow breaking away refuses the flag, and the server is started without it."""
    seen = []

    def popen(command, creationflags=0, **kwargs):
        seen.append(creationflags)
        if creationflags & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return object()

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert local_endpoint._start_apart(["TEST"], cwd=tmp_path)
    assert len(seen) == 2
    assert seen[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB and not seen[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert all(flags & subprocess.CREATE_NO_WINDOW and flags & subprocess.CREATE_NEW_PROCESS_GROUP for flags in seen)


def test_a_resident_server_names_itself_resident_and_ends_when_idle(entry, monkeypatch):
    monkeypatch.setattr(resident_entry, "IDLE_CHECK_SECONDS", 0.05)
    _runtime_config(entry)
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("code", resident_entry.main(
        ["--home", str(entry), "--host", "workbuddy", "--idle-seconds", "1.0"])), daemon=True)
    worker.start()
    name = local_endpoint.endpoints(entry) / f"{os.getpid()}.json"
    deadline = time.monotonic() + 10
    while not name.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(name.read_text(encoding="utf-8"))["resident"] is True
    # A second start of the same entry and client gives way while the first serves.
    assert resident_entry.main(["--home", str(entry), "--host", "workbuddy", "--idle-seconds", "1.0"]) == 0
    worker.join(15)
    assert not worker.is_alive() and box["code"] == 0
    assert not name.exists(), "an idle resident server took its name with it"


def test_a_recall_puts_off_a_resident_server_s_idle_end(entry):
    endpoint = local_endpoint.serve(entry, "workbuddy", warm=False, resident=True)
    assert endpoint is not None
    try:
        before = endpoint.last_used
        time.sleep(0.05)
        try:
            endpoint.recall({"payload": {"hook_event_name": "Stop"}, "current_refs": [], "gaps": [], "remaining": 1.0})
        except Exception:  # noqa: BLE001 - what the recall answers does not matter here
            pass
        assert endpoint.last_used > before
    finally:
        endpoint.stop()


def test_stopping_an_entry_s_resident_servers_ends_them_and_takes_their_names(entry, tmp_path):
    """For an upgrade or an uninstall: a resident server runs from the package that would be replaced.  Its name
    holds its own process id, as a server writes it, which a venv's launcher is not."""
    from scope_recall.runtime.process_probe import probe_process

    said = tmp_path / "TEST-sleeper-pid"
    sleeper = subprocess.Popen([sys.executable, "-c", "import os, sys, time; open(sys.argv[1], 'w').write(str("
                                "os.getpid())); time.sleep(60)", str(said)],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 10
        while not (said.exists() and said.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(said.read_text())
        name = _name(entry, pid=pid, port=4107, resident=True, start=probe_process(pid).start_token)
        assert local_endpoint.stop_residents(entry, "workbuddy") == [pid]
        assert sleeper.wait(10) is not None
        assert not probe_process(pid).running
        assert not name.exists()
        assert local_endpoint.stop_residents(entry, "workbuddy") == []
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_the_resident_command_says_and_stops(entry, capsys):
    from scope_recall.maintenance import resident

    _runtime_config(entry, resident_recall_minutes=45)
    assert resident.main(["status", "--home", str(entry), "--host", "workbuddy"]) == 0
    said = json.loads(capsys.readouterr().out)
    assert said["resident_recall_minutes"] == 45 and said["running"] == []
    assert resident.main(["stop", "--home", str(entry), "--host", "workbuddy"]) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] == []


def test_the_mcp_server_of_a_client_that_keeps_a_resident_server_starts_one_and_answers_no_hook(entry, monkeypatch):
    """Warmed with every conversation, each MCP server held a vector helper of its own beside the resident one."""
    from scope_recall.adapters.codex import mcp_entry

    _runtime_config(entry)
    calls = []
    monkeypatch.setattr(local_endpoint, "serve", lambda *args, **kwargs: calls.append("serve"))
    monkeypatch.setattr(local_endpoint, "ensure_resident",
                        lambda home, host, *, minutes, env_file=None: calls.append(("resident", host, minutes)))

    class _Server:
        class server:
            @staticmethod
            def run(transport):
                calls.append(("run", transport))

    monkeypatch.setattr(mcp_entry, "build_server", lambda *args, **kwargs: _Server())
    assert mcp_entry.main(["--home", str(entry), "--host", "workbuddy"]) == 0
    assert calls == [("resident", "workbuddy", 120), ("run", "stdio")]


def test_a_prompt_hook_starts_a_resident_server_after_its_answer(entry, monkeypatch, capsys):
    from scope_recall.adapters.codex import hook_entry

    _runtime_config(entry)
    order = []
    monkeypatch.setattr(local_endpoint, "ensure_resident",
                        lambda home, host, *, minutes, env_file=None: (order.append(("resident", host, minutes)),
                                                                       "started")[1])
    real_emit = hook_entry.emit_result
    monkeypatch.setattr(hook_entry, "emit_result", lambda *args, **kwargs: (order.append("answer"),
                                                                            real_emit(*args, **kwargs))[1])
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-wb", "prompt": "TEST 你好",
               "cwd": "C:/TEST/work", "transcript_path": "C:/TEST/projects/c--TEST-work/TEST-wb.jsonl"}
    monkeypatch.setattr(sys, "stdin", type("In", (), {"buffer": type("B", (), {
        "read": staticmethod(lambda size: json.dumps(payload).encode("utf-8"))})()})())
    assert hook_entry.main(["--home", str(entry), "--host", "workbuddy"]) == 0
    assert order == ["answer", ("resident", "workbuddy", 120)]
    assert "CODEX_RECALL_RESIDENT_START:started" in capsys.readouterr().err


def test_a_server_s_start_warms_its_query_embedding_once_and_its_keep_warm_does_not(monkeypatch):
    """Warmed by the store alone, a cold server's first two recalls lost their vector search to the embedding."""
    warmed = []

    class Handler:
        runtime_ready = True

        def warm_vectors(self, seconds):
            warmed.append("vectors")

        def warm_embedding(self, seconds):
            warmed.append("embedding")

        def close(self):
            pass

    monkeypatch.setattr(local_endpoint, "KEEP_WARM_IDLE_SECONDS", 0.0)
    monkeypatch.setattr(local_endpoint, "KEEP_WARM_CHECK_SECONDS", 0.05)
    kept = local_endpoint.KeptRecaller(Handler)
    try:
        kept.warm(5.0)
        deadline = time.monotonic() + 5
        while warmed.count("vectors") < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        kept.close()
    assert warmed[:2] == ["vectors", "embedding"] and warmed.count("embedding") == 1, warmed
