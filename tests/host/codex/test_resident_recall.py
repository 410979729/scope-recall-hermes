"""A client's resident prompt recall server (``adapters/codex/resident_entry``, ``local_endpoint.ensure_resident``).

WorkBuddy starts the entry's MCP server with each conversation's agent process, and a prompt that started one met a
server still opening its vector store: a cold server answered with its vector search 12.7 s after its start, past the
prompt hook's 6 s.  A resident server outlives those processes, and hooks ask it first.  Sources are synthetic.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

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
from scope_recall.runtime.process_probe import probe_process


def _own_start_time():
    return probe_process(os.getpid()).start_token


NOW = "2026-10-03T20:00:00Z"
AGENT = "TEST-agent"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: Where a process's start time cannot be read (macOS) a stop ends no process, and the tests that stop one do not apply.
NEEDS_START_TIME = pytest.mark.skipif(_own_start_time() is None, reason="no process start time here: a stop ends none")
#: A resident server as ``ensure_resident`` starts one, bound to this checkout (``tests/sitecustomize.py``, so no
#: ``-I``) and with no LanceDB helper prestarted: the TEST store has no vectors.
RESIDENT = ("import sys\n"
            "from scope_recall.vector import process_store\n"
            "process_store.prestart = lambda **kwargs: None\n"
            "from scope_recall.adapters.codex import resident_entry\n"
            "raise SystemExit(resident_entry.main(sys.argv[1:]))\n")


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


def _wait(predicate, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Running:
    running = True
    start_token = "TEST-start"


class _Holder:
    """The entry's lock held by a thread of this process, as a running resident server holds it."""

    def __init__(self, home, host="workbuddy"):
        from scope_recall.core.file_lock import advisory_file_lock

        self._release = threading.Event()
        held = threading.Event()
        lock = local_endpoint.resident_lock(home, host)
        lock.parent.mkdir(parents=True, exist_ok=True)

        def hold():
            with advisory_file_lock(lock, timeout_seconds=0):
                held.set()
                self._release.wait(30)

        self._thread = threading.Thread(target=hold, daemon=True)
        self._thread.start()
        assert held.wait(5)

    def release(self):
        self._release.set()
        self._thread.join(5)


def _sleeper(said):
    """A process of its own that says its id and sleeps, as a resident server's interpreter."""
    process = subprocess.Popen([sys.executable, "-c", "import os, sys, time; open(sys.argv[1], 'w').write(str("
                                "os.getpid())); time.sleep(60)", str(said)], creationflags=NO_WINDOW)
    assert _wait(lambda: said.exists() and said.read_text(), 10)
    return process, int(said.read_text())


class _Children:
    """Every process a test starts through ``subprocess.Popen``, ended at its end, and the ids it says."""

    def __init__(self, monkeypatch):
        self.started: list[subprocess.Popen] = []
        self.pids: set[int] = set()
        real = subprocess.Popen

        def recording(*args, **kwargs):
            process = real(*args, **kwargs)
            self.started.append(process)
            return process

        monkeypatch.setattr(subprocess, "Popen", recording)

    def end_all(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for process in self.started:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                pass


def _resident_names(home, host="workbuddy"):
    names = []
    for path in local_endpoint.endpoints(home).glob("*.json"):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if info.get("resident") is True and info.get("host") == host:
            names.append(info)
    return names


def _in_thread(argv):
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("code", resident_entry.main(argv)), daemon=True)
    worker.start()
    return worker, box


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
    """Whether one runs is whether the entry's lock is held, whatever the names say; one running is marked in use."""
    from scope_recall.runtime import process_probe

    started = []
    monkeypatch.setattr(local_endpoint, "_start_apart", lambda command, cwd: (started.append(command), True)[1])
    env_file = tmp_path / "TEST.env"
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=0) == "off"
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120, env_file=env_file) == "started"
    assert started == [[sys.executable, "-I", "-B", "-m", "scope_recall.adapters.codex.resident_entry",
                        "--home", str(entry), "--host", "workbuddy", "--detach", "--env-file", str(env_file)]]
    # The next prompt's hook, while that one still starts: no second start.
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120) == "recent"
    assert len(started) == 1
    alive = local_endpoint.resident_alive(entry, "workbuddy")
    holder = _Holder(entry)
    try:
        assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120) == "running"
        assert alive.exists(), "a client process that finds it running marks the client in use"
    finally:
        holder.release()
    assert len(started) == 1
    # A name of a live resident process with no lock held is not one running (a name outlives its server).
    monkeypatch.setattr(process_probe, "probe_process", lambda pid: _Running())
    _name(entry, pid=43, port=4103, resident=True)
    assert not local_endpoint.resident_running(entry, "workbuddy")


def test_a_stamp_from_the_future_is_stale_and_of_two_starters_at_once_one_starts(entry, monkeypatch):
    """A stamp written before the clock was set back held off every start until the clock passed it.  Two starters
    that both looked before either wrote both started one (review of 3.6.0rc1)."""
    started = []
    monkeypatch.setattr(local_endpoint, "_start_apart", lambda command, cwd: (started.append(command), True)[1])
    folder = local_endpoint.endpoints(entry)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = folder / "resident-workbuddy.start"
    stamp.write_text("1", encoding="ascii")
    later = time.time() + 3600
    os.utime(stamp, (later, later))
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120) == "started"
    assert len(started) == 1
    # Another starter makes the stamp between this one's look and its own make.
    earlier = time.time() - 600
    os.utime(stamp, (earlier, earlier))
    real_open = os.open

    def other_starter_first(path, flags, *args):
        if Path(path) == stamp:
            stamp.write_text("2", encoding="ascii")
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", other_starter_first)
    assert local_endpoint.ensure_resident(entry, "workbuddy", minutes=120) == "recent"
    assert len(started) == 1


def test_a_resident_server_of_another_version_or_client_is_not_this_one(entry, monkeypatch):
    from scope_recall.runtime import process_probe

    monkeypatch.setattr(process_probe, "probe_process", lambda pid: _Running())
    _name(entry, pid=44, port=4104, resident=True, version="0.0.1")
    _name(entry, pid=45, port=4105, resident=True, host="codex")
    _name(entry, pid=46, port=4106, resident=False)
    assert local_endpoint._residents(entry, "workbuddy") == []
    assert [(int(info["pid"]), proven) for _paths, info, proven
            in local_endpoint._residents(entry, "workbuddy", any_version=True)] == [(44, True)]
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    assert local_endpoint.stop_residents(entry, "workbuddy", other_versions=True) == [44]
    assert killed == [44]


def test_a_name_whose_process_id_another_process_took_is_neither_counted_nor_stopped(entry, monkeypatch):
    from scope_recall.runtime import process_probe

    class _Another:
        running = True
        start_token = "TEST-another-start"

    monkeypatch.setattr(process_probe, "probe_process", lambda pid: _Another())
    name = _name(entry, pid=47, port=4107, resident=True)
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    assert local_endpoint._residents(entry, "workbuddy", any_version=True) == []
    assert not name.exists(), "its name is removed"
    _name(entry, pid=47, port=4107, resident=True)
    assert local_endpoint.stop_residents(entry, "workbuddy") == [] and killed == []


def test_a_resident_server_whose_identity_cannot_be_proven_is_never_stopped(entry, monkeypatch, capsys):
    """macOS gives no process start time: a name left by a server that died without removing it holds an id any of
    the user's processes may have taken since, and ``resident stop`` sent that process SIGTERM (review of 3.6.0rc1)."""
    from scope_recall.maintenance import resident
    from scope_recall.runtime import process_probe

    class _LiveNoStart:
        running = True
        start_token = None

    monkeypatch.setattr(process_probe, "probe_process", lambda pid: _LiveNoStart())
    _name(entry, pid=4242, port=4112, resident=True, start=None)
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    assert local_endpoint.stop_residents(entry, "workbuddy") == [] and killed == []
    assert resident.main(["stop", "--home", str(entry), "--host", "workbuddy"]) == 0
    said = json.loads(capsys.readouterr().out)
    assert said["stopped"] == [] and [(server["pid"], server["verified"]) for server in said["servers"]] == \
        [(4242, False)]
    assert killed == []


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


def test_a_start_that_fails_otherwise_than_by_the_os_is_tried_once_more_or_reported(monkeypatch, tmp_path):
    """Popen can fail with more than OSError; the start is then tried without breaking away on Windows, and reported
    as not started elsewhere, never raised into the MCP server or the hook (review of 3.6.0rc1)."""
    seen = []

    def popen(command, creationflags=0, **kwargs):
        seen.append(creationflags)
        if len(seen) == 1:
            raise ValueError("TEST: not an OSError")
        return object()

    monkeypatch.setattr(subprocess, "Popen", popen)
    if os.name == "nt":
        assert local_endpoint._start_apart(["TEST"], cwd=tmp_path) and len(seen) == 2
    else:
        assert not local_endpoint._start_apart(["TEST"], cwd=tmp_path) and len(seen) == 1


def test_a_detached_start_starts_the_server_from_a_process_that_ends_at_once(entry, monkeypatch, tmp_path):
    """Started by the MCP server, which lives as long as the conversation, the server was its child, and ending the
    conversation's process tree ended it (measured 2026-10-03).  ``--detach`` starts it from a process that ends."""
    started = []
    monkeypatch.setattr(local_endpoint, "_start_apart", lambda command, cwd: (started.append((command, cwd)), True)[1])
    env_file = tmp_path / "TEST.env"
    assert resident_entry.main(["--home", str(entry), "--host", "workbuddy", "--detach",
                                "--env-file", str(env_file)]) == 0
    assert started == [([sys.executable, "-I", "-B", "-m", "scope_recall.adapters.codex.resident_entry",
                         "--home", str(entry), "--host", "workbuddy", "--env-file", str(env_file)],
                        local_endpoint.endpoints(entry))]
    assert not (local_endpoint.endpoints(entry) / f"{os.getpid()}.json").exists(), "the detaching process served"


def test_a_resident_server_names_itself_resident_and_ends_when_idle(entry, monkeypatch):
    monkeypatch.setattr(resident_entry, "IDLE_CHECK_SECONDS", 0.05)
    _runtime_config(entry)
    worker, box = _in_thread(["--home", str(entry), "--host", "workbuddy", "--idle-seconds", "5.0"])
    name = local_endpoint.endpoints(entry) / f"{os.getpid()}.json"
    record = local_endpoint.resident_record(entry, "workbuddy")
    assert _wait(lambda: name.exists() and record.exists(), 10)
    assert json.loads(name.read_text(encoding="utf-8"))["resident"] is True
    assert json.loads(record.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert local_endpoint.resident_running(entry, "workbuddy")
    # A second start of the same entry, client and version gives way at once while the first serves; serving, it
    # would have run its own idle seconds.
    second = time.monotonic()
    assert resident_entry.main(["--home", str(entry), "--host", "workbuddy", "--idle-seconds", "5.0"]) == 0
    assert time.monotonic() - second < 1.0, "a second resident server served beside the first"
    assert worker.is_alive()
    worker.join(20)
    assert not worker.is_alive() and box["code"] == 0
    assert not name.exists() and not record.exists(), "an idle resident server took its name and record with it"


def test_a_resident_server_ends_once_its_package_is_replaced_or_its_minutes_are_0(entry, monkeypatch):
    """After an upgrade the old server kept the entry's lock against the new version's while hooks asked it nothing;
    set to 0 to free its memory, it kept serving and every prompt put its end off (review of 3.6.0rc1)."""
    monkeypatch.setattr(resident_entry, "IDLE_CHECK_SECONDS", 0.05)
    _runtime_config(entry)
    replaced = threading.Event()
    monkeypatch.setattr(resident_entry, "_package_replaced", replaced.is_set)
    name = local_endpoint.endpoints(entry) / f"{os.getpid()}.json"
    worker, box = _in_thread(["--home", str(entry), "--host", "workbuddy", "--idle-seconds", "60"])
    assert _wait(name.exists, 10)
    time.sleep(0.2)
    assert worker.is_alive()
    replaced.set()
    worker.join(10)
    assert not worker.is_alive() and box["code"] == 0 and not name.exists()

    replaced.clear()
    worker, box = _in_thread(["--home", str(entry), "--host", "workbuddy"])  # WorkBuddy's default, 120 minutes
    assert _wait(name.exists, 10)
    time.sleep(0.2)
    assert worker.is_alive()
    _runtime_config(entry, resident_recall_minutes=0)
    worker.join(10)
    assert not worker.is_alive() and box["code"] == 0 and not name.exists()


def test_the_package_on_disk_is_the_one_a_server_runs_until_it_is_replaced(monkeypatch):
    from scope_recall.runtime import running_code

    assert not resident_entry._package_replaced()
    monkeypatch.setattr(resident_entry, "version_on_disk", lambda path: "0.0.1")
    assert resident_entry._package_replaced()
    monkeypatch.setattr(resident_entry, "version_on_disk", lambda path: None)
    assert resident_entry._package_replaced(), "a package taken away is not the one it runs"
    assert running_code.version_on_disk(Path(running_code.__file__).resolve().parents[1]) is not None


@NEEDS_START_TIME
def test_a_starting_resident_server_stops_one_of_another_version(entry, tmp_path, monkeypatch):
    """A server left from before an upgrade held the entry's lock, hooks skipped it as another version, and every
    server of the new version gave way to it until its idle end (review of 3.6.0rc1)."""
    from scope_recall.runtime.process_probe import probe_process

    monkeypatch.setattr(resident_entry, "IDLE_CHECK_SECONDS", 0.05)
    _runtime_config(entry)
    folder = local_endpoint.endpoints(entry)
    folder.mkdir(parents=True, exist_ok=True)
    said = tmp_path / "TEST-holder-pid"
    holder = subprocess.Popen(
        [sys.executable, "-B", "-c",
         "import os, sys, time\n"
         "from pathlib import Path\n"
         "from scope_recall.core.file_lock import advisory_file_lock\n"
         "with advisory_file_lock(Path(sys.argv[1]), timeout_seconds=0):\n"
         "    Path(sys.argv[2]).write_text(str(os.getpid()))\n"
         "    time.sleep(60)\n",
         str(local_endpoint.resident_lock(entry, "workbuddy")), str(said)], creationflags=NO_WINDOW)
    try:
        assert _wait(lambda: said.exists() and said.read_text())
        pid = int(said.read_text())
        _name(entry, pid=pid, port=4111, resident=True, start=probe_process(pid).start_token, version="3.6.0rc0")
        assert local_endpoint.resident_running(entry, "workbuddy")
        worker, box = _in_thread(["--home", str(entry), "--host", "workbuddy", "--idle-seconds", "2.0"])
        assert holder.wait(10) is not None, "the older server was stopped"
        assert _wait((folder / f"{os.getpid()}.json").exists, 10), "and this version's serves"
        worker.join(20)
        assert not worker.is_alive() and box["code"] == 0
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(10)


def test_a_live_client_process_puts_off_a_resident_server_s_idle_end(tmp_path):
    alive = tmp_path / "resident-workbuddy.alive"
    last_used = time.monotonic() - 100
    assert 99 < resident_entry._idle_seconds(last_used, alive) < 102, "no mark: its last recall"
    alive.write_text("", encoding="ascii")
    assert resident_entry._idle_seconds(last_used, alive) < 2
    for age in (200, -3600):  # an older mark, and one from a clock set back an hour
        stamp = time.time() - age
        os.utime(alive, (stamp, stamp))
        assert 99 < resident_entry._idle_seconds(last_used, alive) < 102, age


def test_a_recall_puts_off_a_resident_server_s_idle_end(entry):
    endpoint = local_endpoint.serve(entry, "workbuddy", warm=False, resident=True)
    assert endpoint is not None
    try:
        before = endpoint.last_used
        time.sleep(0.05)

        def recall(**kwargs):
            try:
                endpoint.recall({"payload": {"hook_event_name": "Stop"}, "current_refs": [], "gaps": [],
                                 "remaining": 1.0}, **kwargs)
            except Exception:  # noqa: BLE001 - what the recall answers does not matter here
                pass

        recall()
        latest = endpoint.last_used
        assert latest > before
        recall(received=latest - 30)  # of two at once, the one received first came here last
        assert endpoint.last_used == latest
    finally:
        endpoint.stop()


@NEEDS_START_TIME
def test_stopping_an_entry_s_resident_servers_ends_them_and_takes_their_names(entry, tmp_path):
    """For an upgrade or an uninstall: a resident server runs from the package that would be replaced.  Its name
    holds its own process id, as a server writes it, which a venv's launcher is not.  A hook removes the name of a
    server that did not prove itself in time; its record beside the lock still says it (review of 3.6.0rc1)."""
    from scope_recall._version import __version__
    from scope_recall.runtime.process_probe import probe_process

    sleeper, pid = _sleeper(tmp_path / "TEST-sleeper-pid")
    try:
        name = _name(entry, pid=pid, port=4107, resident=True, start=probe_process(pid).start_token)
        assert local_endpoint.stop_residents(entry, "workbuddy") == [pid]
        assert sleeper.wait(10) is not None
        assert not probe_process(pid).running
        assert not name.exists()
        assert local_endpoint.stop_residents(entry, "workbuddy") == []
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
    sleeper, pid = _sleeper(tmp_path / "TEST-recorded-pid")
    try:
        record = local_endpoint.resident_record(entry, "workbuddy")
        record.write_text(json.dumps({"host": "workbuddy", "pid": pid, "start": probe_process(pid).start_token,
                                      "version": __version__}), encoding="utf-8")
        assert local_endpoint.stop_residents(entry, "workbuddy") == [pid]
        assert sleeper.wait(10) is not None and not record.exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_the_resident_command_says_and_stops(entry, capsys):
    from scope_recall.maintenance import resident

    _runtime_config(entry, resident_recall_minutes=45)
    assert resident.main(["status", "--home", str(entry), "--host", "workbuddy"]) == 0
    said = json.loads(capsys.readouterr().out)
    assert (said["resident_recall_minutes"], said["running"], said["servers"]) == (45, False, [])
    holder = _Holder(entry)
    try:
        assert resident.main(["status", "--home", str(entry), "--host", "workbuddy"]) == 0
        assert json.loads(capsys.readouterr().out)["running"] is True
    finally:
        holder.release()
    assert resident.main(["stop", "--home", str(entry), "--host", "workbuddy"]) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] == []


def test_the_mcp_server_of_a_client_that_keeps_a_resident_server_keeps_one_and_answers_no_hook(entry, monkeypatch):
    """Warmed with every conversation, each MCP server held a vector helper of its own beside the resident one."""
    from scope_recall.adapters.codex import mcp_entry

    _runtime_config(entry)
    calls = []
    asked = threading.Event()
    monkeypatch.setattr(local_endpoint, "serve", lambda *args, **kwargs: calls.append("serve"))

    def ensure(home, host, *, minutes, env_file=None):
        calls.append(("resident", host, minutes))
        asked.set()
        return "started"

    monkeypatch.setattr(local_endpoint, "ensure_resident", ensure)
    real_keep, kept = local_endpoint.keep_resident, {}
    monkeypatch.setattr(local_endpoint, "keep_resident",
                        lambda *args, **kwargs: kept.setdefault("stop", real_keep(*args, **kwargs)))

    class _Server:
        class server:
            @staticmethod
            def run(transport):
                asked.wait(5)
                calls.append(("run", transport))

    monkeypatch.setattr(mcp_entry, "build_server", lambda *args, **kwargs: _Server())
    assert mcp_entry.main(["--home", str(entry), "--host", "workbuddy"]) == 0
    assert calls == [("resident", "workbuddy", 120), ("run", "stdio")]
    assert kept["stop"].is_set(), "it keeps none once it ends"


def test_a_client_s_live_mcp_server_keeps_a_resident_server_and_nothing_it_meets_ends_it(entry, monkeypatch, capsys):
    """Ended an idle while after its last prompt, the resident left a still-open conversation's next prompt colder than
    the conversation's own server had kept it; an error starting it ended the MCP server before it served a tool
    (review of 3.6.0rc1).  The minutes are read at each pass."""
    _runtime_config(entry)
    calls = []

    def ensure(home, host, *, minutes, env_file=None):
        calls.append(minutes)
        if len(calls) == 1:
            raise RuntimeError("TEST")
        return "running"

    monkeypatch.setattr(local_endpoint, "ensure_resident", ensure)
    stop = local_endpoint.keep_resident(entry, "workbuddy", every=0.02)
    try:
        assert _wait(lambda: len(calls) >= 3, 5)
        _runtime_config(entry, resident_recall_minutes=0)
        time.sleep(0.1)  # a pass that read the minutes before ends
        seen = len(calls)
        time.sleep(0.2)
        assert len(calls) == seen, "at 0 it starts none"
    finally:
        stop.set()
    assert calls[:3] == [120, 120, 120]
    assert "SCOPE_RECALL_RESIDENT_START:RuntimeError" in capsys.readouterr().err


def _hook(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", type("In", (), {"buffer": type("B", (), {
        "read": staticmethod(lambda size: json.dumps(payload).encode("utf-8"))})()})())


def test_a_prompt_hook_starts_a_resident_server_after_its_answer(entry, monkeypatch, capsys, tmp_path):
    from scope_recall.adapters.codex import hook_entry

    _runtime_config(entry)
    env_file = tmp_path / "TEST.env"
    env_file.write_text("", encoding="utf-8")
    order = []
    monkeypatch.setattr(local_endpoint, "ensure_resident",
                        lambda home, host, *, minutes, env_file=None: (order.append(("resident", host, minutes,
                                                                                     env_file)), "started")[1])
    real_emit = hook_entry.emit_result
    monkeypatch.setattr(hook_entry, "emit_result", lambda *args, **kwargs: (order.append("answer"),
                                                                            real_emit(*args, **kwargs))[1])
    _hook(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-wb", "prompt": "TEST 你好",
                        "cwd": "C:/TEST/work", "transcript_path": "C:/TEST/projects/c--TEST-work/TEST-wb.jsonl"})
    assert hook_entry.main(["--home", str(entry), "--host", "workbuddy", "--env-file", str(env_file)]) == 0
    assert order == ["answer", ("resident", "workbuddy", 120, env_file)]
    assert "CODEX_RECALL_RESIDENT_START:started" in capsys.readouterr().err


def test_a_hook_other_than_a_prompt_s_starts_no_resident_server(entry, monkeypatch, capsys):
    from scope_recall.adapters.codex import hook_entry

    _runtime_config(entry)
    started = []
    monkeypatch.setattr(local_endpoint, "ensure_resident",
                        lambda home, host, *, minutes, env_file=None: (started.append(host), "started")[1])
    _hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "TEST-wb", "cwd": "C:/TEST/work",
                        "transcript_path": "C:/TEST/projects/c--TEST-work/TEST-wb.jsonl"})
    assert hook_entry.main(["--home", str(entry), "--host", "workbuddy"]) == 0
    assert started == []
    assert "CODEX_RECALL_RESIDENT_START" not in capsys.readouterr().err


def test_a_server_s_start_warms_its_query_embedding_once_and_its_keep_warm_does_not(monkeypatch):
    """Warmed by the store alone, a cold server's first two recalls lost their vector search to the embedding.  The
    embedding's warming has its own short time: it holds the kept handler (review of 3.6.0rc1)."""
    warmed = []

    class Handler:
        runtime_ready = True

        def warm_vectors(self, seconds):
            warmed.append("vectors")

        def warm_embedding(self, seconds):
            warmed.append(("embedding", seconds))

        def close(self):
            pass

    monkeypatch.setattr(local_endpoint, "KEEP_WARM_IDLE_SECONDS", 0.0)
    monkeypatch.setattr(local_endpoint, "KEEP_WARM_CHECK_SECONDS", 0.05)
    kept = local_endpoint.KeptRecaller(Handler)
    try:
        kept.warm(60.0)
        assert _wait(lambda: warmed.count("vectors") >= 3, 5)
    finally:
        kept.close()
    assert warmed[:2] == ["vectors", ("embedding", local_endpoint.EMBEDDING_WARM_SECONDS)], warmed
    assert local_endpoint.EMBEDDING_WARM_SECONDS == 10.0
    assert sum(isinstance(item, tuple) for item in warmed) == 1, warmed


def test_a_handler_s_embedding_warming_asks_its_runtime():
    """The kept handler's warming looks the method up by name: a handler without it warmed no embedding at all."""
    from scope_recall.adapters.codex.handler import CodexHookHandler

    asked = []
    handler = CodexHookHandler.__new__(CodexHookHandler)
    handler._ensure_host_runtime = lambda: None
    handler._host_runtime = type("Host", (), {"_runtime": type("Runtime", (), {
        "warm_query_embedding": staticmethod(lambda seconds: asked.append(seconds))})()})()
    handler.warm_embedding(3.0)
    assert asked == [3.0]


@NEEDS_START_TIME
def test_two_real_resident_servers_settle_on_one_across_processes_and_a_stop_ends_it(entry, monkeypatch, capsys):
    """Within one process the file lock's thread lock alone keeps a second out; between processes the OS lock must.
    ``resident stop`` ends the one that serves, through the id it says, and says so once its lock is let go."""
    from scope_recall.maintenance import resident

    _runtime_config(entry)
    children = _Children(monkeypatch)
    folder = local_endpoint.endpoints(entry)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for _ in range(2):
            assert local_endpoint._start_apart([sys.executable, "-B", "-c", RESIDENT, "--home", str(entry), "--host",
                                                "workbuddy", "--idle-seconds", "60"], cwd=folder)
        assert _wait(lambda: len(_resident_names(entry)) >= 1, 30)
        children.pids.update(int(info["pid"]) for info in _resident_names(entry))
        assert _wait(lambda: sum(process.poll() is None for process in children.started) == 1, 15), \
            "the second gave way and ended"
        assert [process.returncode for process in children.started if process.poll() is not None] == [0]
        names = _resident_names(entry)
        assert len(names) == 1 and local_endpoint.resident_running(entry, "workbuddy")
        assert resident.main(["stop", "--home", str(entry), "--host", "workbuddy"]) == 0
        said = json.loads(capsys.readouterr().out)
        assert (said["stopped"], said["running"], said["servers"]) == ([int(names[0]["pid"])], False, [])
        assert _wait(lambda: all(process.poll() is not None for process in children.started), 10)
    finally:
        children.end_all()


@pytest.mark.skipif(os.name != "nt", reason="the hook's pipe as WorkBuddy reads it")
def test_a_hook_that_starts_a_resident_server_closes_its_output_at_once(entry, tmp_path):
    """WorkBuddy reads the hook's output until the pipe closes.  Started with the hook's handles, the resident would
    hold it open until its own end, two hours."""
    from scope_recall.runtime.process_probe import probe_process

    said = tmp_path / "TEST-sleeper-pid"
    hook = ("import sys\n"
            "from pathlib import Path\n"
            "from scope_recall.adapters.codex import local_endpoint\n"
            "sleeper = ('import os, sys, time; open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(30)')\n"
            "local_endpoint._start_apart([sys.executable, '-c', sleeper, sys.argv[2]], cwd=Path(sys.argv[1]))\n"
            "sys.stdout.write('{}')\n")
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, "-B", "-c", hook, str(tmp_path), str(said)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    try:
        out, _err = process.communicate(timeout=20)
        elapsed = time.monotonic() - started
        assert _wait(lambda: said.exists() and said.read_text(), 10), "the resident stand-in started"
        assert probe_process(int(said.read_text())).running, "and outlives the hook"
    finally:
        if process.poll() is None:
            process.kill()
        if said.exists() and said.read_text():
            try:
                os.kill(int(said.read_text()), signal.SIGTERM)
            except OSError:
                pass
    assert out == b"{}" and elapsed < 15, elapsed
