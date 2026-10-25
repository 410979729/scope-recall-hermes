"""dsh's installed native plugin -> Python forwarder -> loopback HTTP -> real shared store.

All messages and tokens are synthetic. No dsh model, external service or installed host configuration is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
import yaml
from scope_recall.adapters.clients import remote_client, remote_server, transcript
from scope_recall.adapters.codex import remote_client as client_entry
from scope_recall.adapters.hermes.installation import build_installation_manifest
from scope_recall.adapters.hermes.shared_entries import (
    attach_shared_entry,
    attach_shared_record,
    client_entry_record,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)
from scope_recall.maintenance import install_dsh, install_remote
from scope_recall.maintenance.install_common import InstallPlan

TOKEN = "TEST-dsh-remote-token-not-a-credential"
NOW = "2026-09-27T06:00:00Z"
NODE = os.environ.get("SCOPE_RECALL_TEST_NODE") or shutil.which("node")
HARNESS = Path(__file__).resolve().parent / "dsh_harness"


def _node_runs() -> bool:
    """node 20.6 or later (the harness's module hooks) that this tier may start.  The gate lets the host tier run the
    node it found and refuses node to the other tiers, which skip here as test_shared_client's dsh tests do."""
    if NODE is None:
        return False
    try:
        version = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        major, minor = (int(part) for part in version.lstrip("v").split(".")[:2])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return (major, minor) >= (20, 6)


def _hook(client, **payload):
    return remote_client.run_hook(client, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _rows(root):
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        return db.execute(
            "SELECT entry_id, source_event_key, role, origin, content FROM source_events ORDER BY content"
        ).fetchall()


def _request(url, *, data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8")
    events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return json.loads(events[-1] if events else text)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    root = tmp_path / "TEST-store"
    write_shared_payload(root, new_shared_payload(root, agent_id="TEST-agent"))
    owner_home = tmp_path / "TEST-owner"
    owner_home.mkdir()
    attach_shared_entry(
        root,
        build_installation_manifest(
            owner_home, agent_id="TEST-agent", user_id="TEST-owner", agent_workspace="TEST-workspace"
        ),
        entry_id="owner",
        display_name="TEST Owner",
        now=NOW,
    )
    owner = next(a for a in read_shared_payload(root)["entries"][0]["audiences"] if a["kind"] == "owner_private")
    home = tmp_path / "TEST-server-entry"
    attach_shared_record(
        root,
        client_entry_record(
            host="dsh",
            home=home,
            entry_id="remote-dsh",
            display_name="TEST Remote dsh",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert (
        remote_server.main(
            [
                "configure",
                "--home",
                str(home),
                "--host",
                "dsh",
                "--listen",
                "127.0.0.1",
                "--port",
                str(port),
                "--token-sha256",
                hashlib.sha256(TOKEN.encode()).hexdigest(),
            ]
        )
        == 0
    )
    token = tmp_path / "TEST-token"
    token.write_text(TOKEN, encoding="utf-8")
    config = tmp_path / "TEST-client.json"
    config.write_text(
        json.dumps(
            {
                "host": "dsh",
                "url": f"http://127.0.0.1:{port}",
                "token_file": str(token),
                "state_dir": str(tmp_path / "TEST-client-state"),
            }
        ),
        encoding="utf-8",
    )
    client = remote_client.load_client_config(config)
    bodies, records = [], []
    handle = remote_server.handle_request
    dsh_lines = transcript.dsh_lines

    def handle_traced(config, body, **kwargs):
        bodies.append(body)
        return handle(config, body, **kwargs)

    def lines_traced(record):
        records.append(record)
        return dsh_lines(record)

    monkeypatch.setattr(remote_server, "handle_request", handle_traced)
    monkeypatch.setattr(transcript, "dsh_lines", lines_traced)
    server = uvicorn.Server(
        uvicorn.Config(
            remote_server.build_app(remote_server.load_server_config(home, "dsh")),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        health = _request(client["url"] + "/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert health["host"] == "dsh" and health["entry_id"] == "remote-dsh"
        yield root, home, client, bodies, records
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive(), "this test's loopback server must stop"


def _installed(client, tmp_path):
    home = tmp_path / "TEST-dsh-home"
    home.mkdir(exist_ok=True)
    install_remote.install(client, home)
    operations = yaml.safe_load((home / install_dsh.PATCH_FILENAME).read_text(encoding="utf-8"))
    rows = {row["id"]: row for op in operations for row in op.get("insert", [])}
    return home, rows


def test_dsh_remote_record_recall_identity_and_bounds(remote):
    root, home, client, bodies, records = remote
    assert remote_client.HOOK_TIMEOUTS["dsh"] == {"UserPromptSubmit": 9, "Stop": 20}
    _hook(
        client,
        hook_event_name="UserPromptSubmit",
        session_id="TEST-seed",
        turn_id="1",
        prompt="TEST 我的猫叫 Mochi，最爱吃金枪鱼。",
    )
    answer = _hook(
        client, hook_event_name="UserPromptSubmit", session_id="TEST-query", turn_id="1", prompt="TEST 我的猫叫什么？"
    )
    assert "Mochi" in answer["hookSpecificOutput"]["additionalContext"]
    record = [
        {"id": "u1", "role": "user", "text": "TEST nested record user", "time": int(time.time() * 1000) - 3000},
        {
            "id": "a1",
            "role": "assistant",
            "text": "TEST nested record assistant",
            "time": int(time.time() * 1000) - 2000,
        },
    ]
    payload = dict(
        hook_event_name="Stop",
        session_id="TEST-capture",
        record=record,
        entry_id="owner",
        host="codex",
        transcript_path="TEST-never-open-this-path",
    )
    assert _hook(client, **payload) == {"through": 2, "private_rules": False}
    assert bodies[-1]["payload"]["record"] == record and "record" not in bodies[-1]
    assert records[-1] == record, "the existing dsh_lines parser consumed the nested record"
    before = _rows(root)
    assert _hook(client, **payload) == {"through": 2, "private_rules": False} and _rows(root) == before
    assert {row[0] for row in before} == {"remote-dsh"}, "payload cannot rebind the entry/host"
    captured = [row for row in before if "TEST-capture" in row[1]]
    assert {(row[2], row[3]) for row in captured} == {("user", "human_direct"), ("assistant", "assistant_visible")}
    assert _hook(client, **{**payload, "session_id": "TEST-other"}) == {"through": 2, "private_rules": False}
    assert len(_rows(root)) == len(before) + 2, "record ids are session-bound"
    before = _rows(root)
    assert _hook(client, **{**payload, "session_id": ""}) == {}
    assert _hook(client, hook_event_name="SessionEnd", session_id="TEST-capture") == {}
    assert (
        _hook(client, hook_event_name="UserPromptSubmit", session_id="TEST-big", turn_id="1", prompt="X" * 70000) == {}
    )
    assert _rows(root) == before
    client["token_file"].write_text("TEST-wrong-token", encoding="utf-8")
    assert _hook(client, **payload) == {} and _rows(root) == before
    assert not (client["state_dir"] / "spool").exists(), "the Codex forwarder spool is not a dsh queue"
    logs = "\n".join(
        p.read_text(encoding="utf-8") for folder in (home, client["state_dir"]) for p in folder.rglob("*.log")
    )
    assert TOKEN not in logs and "TEST-wrong-token" not in logs
    assert TOKEN not in json.dumps(bodies)


def test_a_fast_client_s_turn_is_stored_once_and_its_reply_sent_again_is_the_one_stored(remote, monkeypatch):
    """A client whose clock runs ten minutes ahead has every time a request carries moved back by its session's lead
    (``remote_server.client_times``), the turn's messages dsh's plugin sends inside its Stop with the hook's own: the
    person's line is the prompt already stored, nothing is dated ahead of this machine's clock, and the reply sent again
    with a later Stop is the reply stored."""
    root, _home, client, _bodies, _records = remote

    def client_now():
        return datetime.now(UTC) + timedelta(minutes=10)

    monkeypatch.setattr(remote_client, "_now", lambda: client_now().isoformat().replace("+00:00", "Z"))
    _hook(
        client, hook_event_name="UserPromptSubmit", session_id="TEST-fast", turn_id="1", prompt="TEST 时钟快了十分钟。"
    )
    said = int((client_now() - timedelta(seconds=5)).timestamp() * 1000)
    stop = dict(
        hook_event_name="Stop",
        session_id="TEST-fast",
        turn_id="1",
        last_assistant_message="TEST 快钟那边的回答。",
        reply_id="a1",
        record=[
            {"id": "u1", "role": "user", "text": "TEST 时钟快了十分钟。", "time": said - 2000},
            {"id": "a1", "role": "assistant", "text": "TEST 快钟那边的回答。", "time": said},
        ],
    )
    assert _hook(client, **stop) == {"through": 2, "private_rules": False}
    assert _hook(client, **stop) == {"through": 2, "private_rules": False}, "sent again"
    latest = datetime.now(UTC) + timedelta(seconds=remote_server.CLOCK_AHEAD_SECONDS)
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        kept = db.execute(
            "SELECT source_event_key, occurred_at FROM source_events WHERE source_event_key LIKE '%:TEST-fast:%'"
        ).fetchall()
        waiting = db.execute("SELECT count(*) FROM capture_inbox").fetchone()[0]
    assert sorted(key.split(":TEST-fast:", 1)[1].rsplit("@", 1)[0] for key, _at in kept) == ["assistant:1", "user:1"]
    assert all(datetime.fromisoformat(at.replace("Z", "+00:00")) <= latest for _key, at in kept), kept
    assert waiting == 0, "nothing left for the store's worker to store under a key of its own"


def test_a_fast_client_s_turn_keeps_its_order_whatever_each_request_s_delay(remote, monkeypatch):
    """A client 90 s fast whose prompt reached this machine 5 s after it was sent, and whose Stop came at once: each
    moved back by its own lead, the reply said 2 s after the prompt would be dated 3 s before it.  Every request of a
    client's session moves back by the lead its first one was found ahead by."""
    root, home, _client, _bodies, _records = remote
    config = remote_server.load_server_config(home, "dsh")
    start = datetime.now(UTC)

    class Clock(datetime):
        at = start

        @classmethod
        def now(cls, tz=None):
            return cls.at

    monkeypatch.setattr(remote_server, "datetime", Clock)

    def client(seconds):  # the client's clock, 90 s fast
        return start + timedelta(seconds=90 + seconds)

    def stamp(moment):
        return moment.isoformat().replace("+00:00", "Z")

    def ms(moment):
        return int(moment.timestamp() * 1000)

    session = {"session_id": "TEST-delays", "turn_id": "1", "cwd": "C:/TEST/work"}
    Clock.at = start + timedelta(seconds=5)
    remote_server.handle_request(
        config,
        {
            "payload": {"hook_event_name": "UserPromptSubmit", "prompt": "TEST 先问一句。", **session},
            "observed_at": stamp(client(0)),
        },
    )
    Clock.at = start + timedelta(seconds=10)
    remote_server.handle_request(
        config,
        {
            "payload": {
                "hook_event_name": "Stop",
                "last_assistant_message": "TEST 两秒后就答了。",
                "reply_id": "a1",
                "record": [
                    {"id": "u1", "role": "user", "text": "TEST 先问一句。", "time": ms(client(0))},
                    {"id": "a1", "role": "assistant", "text": "TEST 两秒后就答了。", "time": ms(client(2))},
                ],
                **session,
            },
            "observed_at": stamp(client(10)),
        },
    )
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        when = dict(
            db.execute(
                "SELECT content, occurred_at FROM source_events WHERE source_event_key LIKE '%:TEST-delays:%'"
            ).fetchall()
        )
    moment = {text: datetime.fromisoformat(at.replace("Z", "+00:00")) for text, at in when.items()}
    assert sorted(moment) == ["TEST 两秒后就答了。", "TEST 先问一句。"], when
    assert moment["TEST 先问一句。"] < moment["TEST 两秒后就答了。"], when


def test_dsh_remote_install_and_mcp(remote, tmp_path, capsys):
    _root, _home, client, _bodies, _records = remote
    home = tmp_path / "TEST-native-home"
    home.mkdir()
    patch = home / install_dsh.PATCH_FILENAME
    original = b"# TEST other plugin\n- id: TEST-other\n  disabled: true\n"
    patch.write_bytes(original)
    assert client_entry.main(["install", "--config", str(client["config"]), "--plugin-dir", str(home)]) == 0
    output = capsys.readouterr().out
    assert TOKEN not in output
    report = json.loads(output.strip().splitlines()[-1])
    assert len(report["written"]) == 2
    assert [Path(p).read_bytes() for p in report["backups"]] == [original]
    assert install_dsh.plugin_path(home).read_bytes() == install_dsh.plugin_source()
    ops = yaml.safe_load(patch.read_text(encoding="utf-8"))
    rows = {row["id"]: row for op in ops for row in op.get("insert", [])}
    assert {"id": "TEST-other", "disabled": True} in ops and install_dsh.upload_off(ops)
    cfg = rows["scope-recall"]["config"]
    assert cfg["remoteConfig"] == client["config"].as_posix() and cfg["home"] == client["state_dir"].as_posix()
    assert "envFile" not in cfg
    mcp = rows["mcp-scope-recall"]["config"]
    assert mcp["transport"] == "streamable-http" and mcp["url"] == client["url"] + "/mcp"
    assert install_remote.install(client, home) == {"written": [], "backups": []}
    headers = {**mcp["headers"], "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "TEST-dsh", "version": "0"},
        },
    }
    assert _request(mcp["url"], data=json.dumps(initialize).encode(), headers=headers)["result"]["serverInfo"]
    tools = _request(
        mcp["url"], data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(), headers=headers
    )
    offered = {tool["name"] for tool in tools["result"]["tools"]}
    assert {"recall", "status"} <= offered
    assert not offered & {"propose_memory", "revise", "forget"}, "dsh names no conversation with a call (#245)"
    with pytest.raises(urllib.error.HTTPError) as denied:
        _request(mcp["url"], data=json.dumps(initialize).encode(), headers={"Content-Type": "application/json"})
    assert denied.value.code == 401
    client["token_file"].write_text("TEST-rotated-token", encoding="utf-8")
    assert install_remote.install(client, home)["written"] == [str(patch)]
    assert "TEST-rotated-token" in patch.read_text(encoding="utf-8")
    with pytest.raises(remote_client.RemoteClientError, match="another Scope Recall entry"):
        install_remote.install({**client, "config": tmp_path / "TEST-different-client.json"}, home)
    local_plan = InstallPlan(
        host="dsh",
        target_plugin_dir=tmp_path / "TEST-local-home",
        instance_root=_home,
        project_root=None,
        agent_id="TEST-agent",
        python_executable=Path(sys.executable),
    )
    local = yaml.safe_load(install_dsh.merged_file(local_plan, tmp_path / "TEST-local-patch.yml"))
    local_rows = {row["id"]: row for op in local for row in op.get("insert", [])}
    assert "remoteConfig" not in local_rows["scope-recall"]["config"]
    assert local_rows["scope-recall"]["config"]["home"] == _home.as_posix()
    assert local_rows["mcp-scope-recall"]["config"]["transport"] == "stdio"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; on Windows the profile's ACLs keep the files private")
def test_a_reinstall_keeps_the_token_bearing_patch_and_its_backup_private(remote, tmp_path, monkeypatch):
    """The patch's MCP row carries the entry's token.  Written under a umask of 022 a reinstall made it readable by
    every local account, and the backup of the patch it replaced as well."""
    _root, _home, client, _bodies, _records = remote
    home = tmp_path / "TEST-private-home"
    home.mkdir()
    patch = home / install_dsh.PATCH_FILENAME
    patch.write_bytes(b"# TEST other plugin\n- id: TEST-other\n  disabled: true\n")
    patch.chmod(0o600)
    umask = os.umask(0o022)
    try:
        first = install_remote.install(client, home)
        client["token_file"].write_text("TEST-rotated-token", encoding="utf-8")
        second = install_remote.install(client, home)
    finally:
        os.umask(umask)
    assert str(patch) in second["written"] and patch.stat().st_mode & 0o077 == 0
    for backup in (*first["backups"], *second["backups"]):
        assert Path(backup).stat().st_mode & 0o077 == 0 and Path(backup).parent.stat().st_mode & 0o077 == 0
    # An installation an older install left open: a readable patch nothing changes, a readable backup, and a
    # readable temporary file an interrupted run left behind.
    patch.chmod(0o644)
    old = Path(second["backups"][0])
    old.chmod(0o644)
    old.parent.chmod(0o755)
    stale = patch.with_name(patch.name + ".tmp")
    stale.write_bytes(b"TEST stale")
    stale.chmod(0o644)
    assert install_remote.install(client, home)["written"] == []
    assert patch.stat().st_mode & 0o077 == 0 and not stale.exists()
    assert old.stat().st_mode & 0o077 == 0 and old.parent.stat().st_mode & 0o077 == 0
    # The new token is written only into a file made for it, owner-only from its creation.
    opened, real_open = [], os.open

    def recording_open(path, flags, mode=0o777, *args, **kwargs):
        opened.append((Path(path).name, flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    client["token_file"].write_text("TEST-rotated-again", encoding="utf-8")
    assert str(patch) in install_remote.install(client, home)["written"]
    created = [(flags, mode) for name, flags, mode in opened if name == patch.name + ".tmp"]
    assert created and all(flags & os.O_EXCL and mode == 0o600 for flags, mode in created)
    assert patch.stat().st_mode & 0o077 == 0


def _plugin(home, config, scenario="turn"):
    result = subprocess.run(
        [
            NODE,
            "--import",
            (HARNESS / "register.mjs").as_uri(),
            str(HARNESS / "harness.mjs"),
            str(install_dsh.plugin_path(home)),
            json.dumps(config),
            scenario,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=110,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert TOKEN not in result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _plugin_rules(spool, value):
    """Seed the metadata answer independently of a live store's state."""
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "private-rules.json").write_text(json.dumps({"private_rules": value}), encoding="utf-8")


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is required")
@pytest.mark.parametrize("cached", [False, None], ids=["stored-false", "fresh"])
def test_plugin_without_known_rules_appends_without_check_spawns(remote, tmp_path, monkeypatch, cached):
    _root, _server_home, client, _bodies, _records = remote
    home, rows = _installed(client, tmp_path)
    spool = tmp_path / "TEST-spool"
    if cached is not None:
        _plugin_rules(spool, cached)
    log = tmp_path / "TEST-hook-calls.jsonl"
    monkeypatch.setenv("SR_FAKE_HOOK", str(HARNESS / "fake_hook.mjs"))
    monkeypatch.setenv("SR_FAKE_HOOK_MODE", "broken")
    monkeypatch.setenv("SR_FAKE_HOOK_LOG", str(log))
    result = _plugin(
        home, {**rows["scope-recall"]["config"], "spool": str(spool), "expectBacklog": True, "waitMs": 1000}
    )
    calls = [json.loads(line)["hook_event_name"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls == ["UserPromptSubmit", "Stop"], "keeping each message must start no hook"
    assert result["spool"] == ["user", "assistant", "assistant", "turn_end"]
    assert result["diskSnapshots"], "ordinary messages are appended during the event callback"


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is required")
def test_plugin_without_known_rules_adopts_old_spool_without_checks(remote, tmp_path, monkeypatch):
    _root, _server_home, client, _bodies, _records = remote
    home, rows = _installed(client, tmp_path)
    spool = tmp_path / "TEST-spool"
    _plugin_rules(spool, False)
    old = spool / "session-TEST-plugin.999999.jsonl"
    old.write_text(
        json.dumps(
            dict(
                k="TEST-line",
                sessionId="session-TEST-plugin",
                id="TEST-1",
                role="user",
                text="TEST public old body",
                time=int(time.time() * 1000),
                cwd="C:/TEST/work",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(old, (time.time() - 130, time.time() - 130))
    log = tmp_path / "TEST-hook-calls.jsonl"
    monkeypatch.setenv("SR_FAKE_HOOK", str(HARNESS / "fake_hook.mjs"))
    monkeypatch.setenv("SR_FAKE_HOOK_MODE", "broken")
    monkeypatch.setenv("SR_FAKE_HOOK_LOG", str(log))
    result = _plugin(
        home, {**rows["scope-recall"]["config"], "spool": str(spool), "expectBacklog": True, "waitMs": 6500}, "sweep"
    )
    assert not old.exists(), "ordinary recovery adopts the orphan even while the server is unreachable"
    assert result["spool"] == ["user"]
    assert [json.loads(line)["hook_event_name"] for line in log.read_text(encoding="utf-8").splitlines()] == ["Stop"]


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is required")
def test_plugin_hook_answer_updates_rule_cache_before_keeping(remote, tmp_path, monkeypatch):
    _root, _server_home, client, _bodies, _records = remote
    home, rows = _installed(client, tmp_path)
    spool = tmp_path / "TEST-spool"
    _plugin_rules(spool, False)
    log = tmp_path / "TEST-hook-calls.jsonl"
    monkeypatch.setenv("SR_FAKE_HOOK", str(HARNESS / "fake_hook.mjs"))
    monkeypatch.setenv("SR_FAKE_HOOK_MODE", "private")
    monkeypatch.setenv("SR_FAKE_PRIVATE_RULES", "true")
    monkeypatch.setenv("SR_FAKE_HOOK_LOG", str(log))
    result = _plugin(
        home, {**rows["scope-recall"]["config"], "spool": str(spool), "expectBacklog": True, "waitMs": 1000}
    )
    assert result["spool"] == []
    assert json.loads((spool / "private-rules.json").read_text(encoding="utf-8")) == {"private_rules": True}
    assert result["diskSnapshots"] == []
    checks = [
        call
        for line in log.read_text(encoding="utf-8").splitlines()
        if (call := json.loads(line))["hook_event_name"] == "PrivateCheck"
    ]
    assert len(checks) == 1 and len(checks[0]["times"]) == 4


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is required")
@pytest.mark.parametrize("mode", ["private", "broken"])
def test_plugin_never_writes_unverified_or_private_plaintext(remote, tmp_path, monkeypatch, mode):
    _root, _server_home, client, _bodies, _records = remote
    home, rows = _installed(client, tmp_path)
    config = rows["scope-recall"]["config"]
    config.update(spool=str(tmp_path / "TEST-spool"), prompt="TEST private disk body", expectBacklog=True, waitMs=1500)
    _plugin_rules(tmp_path / "TEST-spool", True)
    monkeypatch.setenv("SR_FAKE_HOOK", str(HARNESS / "fake_hook.mjs"))
    monkeypatch.setenv("SR_FAKE_HOOK_MODE", mode)
    result = _plugin(home, config)
    assert "TEST private disk body" not in "".join(result["diskSnapshots"])
    assert result["spool"] == []


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is required")
@pytest.mark.parametrize("mode", ["private", "broken"])
def test_plugin_checks_old_spool_before_copying_plaintext(remote, tmp_path, monkeypatch, mode):
    _root, _server_home, client, _bodies, _records = remote
    home, rows = _installed(client, tmp_path)
    config = rows["scope-recall"]["config"]
    spool = tmp_path / "TEST-spool"
    spool.mkdir()
    _plugin_rules(spool, True)
    old = spool / "session-TEST-plugin.999999.jsonl"
    old.write_text(
        json.dumps(
            dict(
                k="TEST-line",
                sessionId="session-TEST-plugin",
                id="TEST-1",
                role="user",
                text="TEST private old body",
                time=int(time.time() * 1000),
                cwd="C:/TEST/work",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(old, (time.time() - 130, time.time() - 130))
    config.update(spool=str(spool), expectBacklog=True, waitMs=8000)
    monkeypatch.setenv("SR_FAKE_HOOK", str(HARNESS / "fake_hook.mjs"))
    monkeypatch.setenv("SR_FAKE_HOOK_MODE", mode)
    result = _plugin(home, config, "sweep")
    if mode == "broken":
        assert old.exists()
        assert "TEST private old body" in old.read_text(encoding="utf-8")
        assert list(spool.glob("*.jsonl")) == [old]
    else:
        assert result["spool"] == []
        assert not any("TEST private old body" in path.read_text(encoding="utf-8") for path in spool.glob("*.jsonl"))


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is not installed, or this tier may not start it")
def test_plugin_shutdown_waits_for_private_writes_and_their_store(tmp_path, monkeypatch):
    """Shutdown sees handed-off writes before disk exists and awaits a store beyond three seconds."""
    home = tmp_path / "TEST-plugin-home"
    plugin = install_dsh.plugin_path(home)
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(install_dsh.plugin_source())
    spool = home / "scope-recall" / "dsh-spool"
    _plugin_rules(spool, True)
    monkeypatch.setenv("SR_FAKE_HOOK", str(HARNESS / "fake_hook.mjs"))
    monkeypatch.setenv("SR_FAKE_PRIVATE_RULES", "true")
    monkeypatch.setenv("SR_FAKE_PRIVATECHECK_DELAY", "100")
    monkeypatch.setenv("SR_FAKE_STOP_DELAY", "1100")
    log = tmp_path / "TEST-hook-calls.jsonl"
    monkeypatch.setenv("SR_FAKE_HOOK_LOG", str(log))
    result = _plugin(home, dict(home=str(home), python=sys.executable, spool=str(spool), storeTimeoutMs=60_000))
    assert result["spool"] == []
    assert result["status"]["lastStore"]["stored"] == 3
    assert result["status"]["lastStore"]["error"] is None
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert any(call.get("fresh_observation") and len(call["times"]) == 4 for call in calls)


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is not installed, or this tier may not start it")
def test_plugin_shutdown_deadline_includes_pending_private_writes(tmp_path, monkeypatch):
    """A private decision that cannot finish shares the shutdown bound with every queued write."""
    home = tmp_path / "TEST-plugin-home"
    plugin = install_dsh.plugin_path(home)
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(install_dsh.plugin_source())
    spool = home / "scope-recall" / "dsh-spool"
    _plugin_rules(spool, True)
    monkeypatch.setenv("SR_FAKE_HOOK", str(HARNESS / "fake_hook.mjs"))
    monkeypatch.setenv("SR_FAKE_PRIVATE_RULES", "true")
    monkeypatch.setenv("SR_FAKE_PRIVATECHECK_DELAY", "60000")
    started = time.monotonic()
    result = _plugin(home, dict(home=str(home), python=sys.executable, spool=str(spool), storeTimeoutMs=2_000))
    assert time.monotonic() - started < 30
    assert result["spool"] == []


@pytest.mark.skipif(not _node_runs(), reason="node 20.6 or later is not installed, or this tier may not start it")
def test_installed_remote_plugin_recalls_and_replays_unacknowledged_stop(remote, tmp_path, monkeypatch):
    from scope_recall.adapters.clients.config import load_shared_client
    from scope_recall.contracts import TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, _home, client, bodies, records = remote
    home, rows = _installed(client, tmp_path)
    config = rows["scope-recall"]["config"]
    spool = client["state_dir"] / "scope-recall" / "dsh-spool"
    config["spool"] = str(spool)
    _plugin_rules(spool, True)
    _hook(
        client,
        hook_event_name="UserPromptSubmit",
        session_id="TEST-seed",
        turn_id="1",
        prompt="TEST 我的猫叫 Mochi，最爱吃金枪鱼。",
    )
    entry = load_shared_client(_home, "dsh")
    context = TrustedContext(entry.to_binding(), "TEST-other", entry.scope_ids, "human_direct")
    core = MemoryCore(CoreConfig(entry.to_binding()))
    MemoryManagement(core, InstallationMaintenance(context)).private_on(target="session", session=context.session_id)
    handle = remote_server.handle_request
    monkeypatch.setattr(
        remote_server,
        "handle_request",
        lambda *args, **kwargs: dict(retry=True, result={}, reason="TEST retry", through=0),
    )
    failed = _plugin(home, {**config, "expectBacklog": True, "waitStatus": "lastStore", "waitMs": 15000})
    assert failed["spool"] == ["user", "assistant", "assistant", "turn_end"]
    assert failed["status"]["lastStore"]["error"] and failed["decisionKept"]
    queued = [
        json.loads(line)
        for file in spool.glob("*.jsonl")
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert queued and all(isinstance(row.get("clock"), dict) and "received_at" in row["clock"] for row in queued)
    assert not any("session-TEST-plugin" in row[1] for row in _rows(root))
    monkeypatch.setattr(remote_server, "handle_request", handle)
    for file in spool.glob("*.jsonl"):
        os.utime(file, (time.time() - 130, time.time() - 130))
    replayed = _plugin(home, config, "sweep")
    assert replayed["spool"] == [] and replayed["status"]["lastStore"]["error"] is None
    assert len([row for row in _rows(root) if "session-TEST-plugin" in row[1]]) == 3
    # A loaded CI runner can take longer than the plugin's default 9 s for a cold recall; the bound itself is not
    # what this test measures.
    successful = _plugin(home, {**config, "recallTimeoutMs": 30_000})
    assert successful["decisionKept"] and "Mochi" in successful["injected"]["text"]
    assert successful["spool"] == [] and successful["status"]["lastStore"]["error"] is None, successful
    assert any(body["payload"].get("record") for body in bodies)
    assert records and all(len(json.dumps(body).encode()) <= 65536 for body in bodies)
    assert not any("runtime context" in row[4] for row in _rows(root))
