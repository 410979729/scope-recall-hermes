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
    assert _hook(client, **payload) == {"through": 2}
    assert bodies[-1]["payload"]["record"] == record and "record" not in bodies[-1]
    assert records[-1] == record, "the existing dsh_lines parser consumed the nested record"
    before = _rows(root)
    assert _hook(client, **payload) == {"through": 2} and _rows(root) == before
    assert {row[0] for row in before} == {"remote-dsh"}, "payload cannot rebind the entry/host"
    captured = [row for row in before if "TEST-capture" in row[1]]
    assert {(row[2], row[3]) for row in captured} == {("user", "human_direct"), ("assistant", "assistant_visible")}
    assert _hook(client, **{**payload, "session_id": "TEST-other"}) == {"through": 2}
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
    assert {"recall", "status"} <= {tool["name"] for tool in tools["result"]["tools"]}
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_installed_remote_plugin_recalls_and_replays_unacknowledged_stop(remote, tmp_path):
    root, _home, client, bodies, records = remote
    home, rows = _installed(client, tmp_path)
    config = rows["scope-recall"]["config"]
    spool = client["state_dir"] / "scope-recall" / "dsh-spool"
    config["spool"] = str(spool)
    _hook(
        client,
        hook_event_name="UserPromptSubmit",
        session_id="TEST-seed",
        turn_id="1",
        prompt="TEST 我的猫叫 Mochi，最爱吃金枪鱼。",
    )
    client["token_file"].write_text("TEST-wrong-token", encoding="utf-8")
    failed = _plugin(home, {**config, "expectBacklog": True, "waitStatus": "lastStore", "waitMs": 15000})
    assert failed["spool"] == ["user", "assistant", "assistant", "turn_end"]
    assert failed["status"]["lastStore"]["error"] and failed["decisionKept"]
    assert not any("session-TEST-plugin" in row[1] for row in _rows(root))
    client["token_file"].write_text(TOKEN, encoding="utf-8")
    for file in spool.glob("*.jsonl"):
        os.utime(file, (time.time() - 130, time.time() - 130))
    replayed = _plugin(home, config, "sweep")
    assert replayed["spool"] == [] and replayed["status"]["lastStore"]["error"] is None
    assert len([row for row in _rows(root) if "session-TEST-plugin" in row[1]]) == 3
    successful = _plugin(home, config)
    assert successful["decisionKept"] and "Mochi" in successful["injected"]["text"]
    assert successful["spool"] == [] and successful["status"]["lastStore"]["error"] is None
    assert any(body["payload"].get("record") for body in bodies)
    assert records and all(len(json.dumps(body).encode()) <= 65536 for body in bodies)
    assert not any("runtime context" in row[4] for row in _rows(root))
