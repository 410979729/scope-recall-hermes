"""A client on another machine: its hooks and MCP tools reach its entry's server over HTTP.

The server runs in this process on 127.0.0.1 with a real shared store; the client posts to it as the hook on the
other machine would.  Sources are synthetic; nothing here is a person's memory.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from scope_recall.adapters.codex import remote_client, remote_server, transcript
from scope_recall.adapters.hermes.installation import (
    attach_shared_entry,
    attach_shared_record,
    build_installation_manifest,
    client_entry_record,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)

NOW = "2026-09-27T06:00:00Z"
AGENT = "TEST-agent"
TOKEN = "TEST-token-0123456789-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "TEST-shared"
    write_shared_payload(root, new_shared_payload(root, agent_id=AGENT))
    home = tmp_path / "TEST-tianshu-home"
    home.mkdir()
    attach_shared_entry(root, build_installation_manifest(home, agent_id=AGENT, user_id="TEST-owner",
                                                          agent_workspace="TEST-workspace"),
                        entry_id="tianshu", display_name="天枢", now=NOW)
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    homes = {}
    for host, entry, name in (("claude-code", "workpc-claude-code", "工作机 Claude Code"),
                              ("codex", "workpc-codex", "工作机 Codex")):
        client = tmp_path / f"TEST-{entry}-home"
        attach_shared_record(root, client_entry_record(
            host=host, home=client, entry_id=entry, display_name=name, attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"], writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"]), now=NOW)
        homes[host] = client
    return root, homes


def _free_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def served(store):
    """Both entries served on loopback, each with its own token, as a client machine sees them."""
    import uvicorn

    root, homes = store
    running = {}
    for host, home in homes.items():
        port = _free_port()
        remote_server.write_server_config(home, host, listen="127.0.0.1", port=port,
                                          token_sha256=hashlib.sha256(f"{TOKEN}-{host}".encode()).hexdigest())
        server = uvicorn.Server(uvicorn.Config(remote_server.build_app(remote_server.load_server_config(home, host)),
                                               host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, f"{host} server did not start"
        running[host] = (server, thread, port)
    yield root, homes, {host: port for host, (_s, _t, port) in running.items()}
    for server, thread, _port in running.values():
        server.should_exit = True
        thread.join(10)


def _client(tmp_path: Path, host: str, port: int, *, token: str | None = None) -> dict:
    state = tmp_path / f"TEST-client-{host}"
    state.mkdir(exist_ok=True)
    token_file = state / "token"
    token_file.write_text(token or f"{TOKEN}-{host}", encoding="utf-8")
    config = state / "client.json"
    config.write_text(json.dumps({"url": f"http://127.0.0.1:{port}", "host": host, "token_file": str(token_file),
                                  "state_dir": str(state / "state")}), encoding="utf-8")
    return remote_client.load_client_config(config)


def _rows(root: Path, entry: str) -> list[tuple]:
    with closing(sqlite3.connect(root / "memory.sqlite3")) as connection:
        return connection.execute(
            "SELECT role, origin, content, occurred_at FROM source_events WHERE entry_id=? ORDER BY content",
            (entry,)).fetchall()


def _hook(config: dict, payload: dict) -> dict:
    return remote_client.run_hook(config, json.dumps(payload).encode("utf-8"))


def _moments():
    start = datetime.now(timezone.utc) - timedelta(seconds=30)
    return lambda seconds: (start + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _line(kind, uuid, stamp, **fields):
    return {"type": kind, "uuid": uuid, "timestamp": stamp, "sessionId": "TEST-work-session", **fields}


def _record(path: Path, *rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def test_a_wrong_token_is_refused_and_nothing_is_stored(served, tmp_path):
    root, _homes, ports = served
    config = _client(tmp_path, "claude-code", ports["claude-code"], token="TEST-not-the-token")
    assert _hook(config, {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-work-session",
                          "prompt_id": "TEST-p1", "prompt": "TEST 这句不该进库", "cwd": "C:/work"}) == {}
    assert _rows(root, "workpc-claude-code") == []
    request = urllib.request.Request(f"http://127.0.0.1:{ports['claude-code']}/health")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 401


def test_claude_code_prompt_and_record_reach_the_entry_once(served, tmp_path):
    root, _homes, ports = served
    config = _client(tmp_path, "claude-code", ports["claude-code"])
    at = _moments()
    _hook(config, {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-work-session", "prompt_id": "TEST-p1",
                   "prompt": "TEST 帮我看一下 QX-17。", "cwd": "C:/work"})
    record = _record(tmp_path / "TEST-work-projects" / "TEST-work-session.jsonl",
                     _line("user", "u1", at(0), origin={"kind": "human"}, promptId="TEST-p1",
                           message={"role": "user", "content": "TEST 帮我看一下 QX-17。"}),
                     _line("assistant", "a1", at(1), message={"role": "assistant", "model": "TEST-model",
                                                              "content": [{"type": "text", "text": "TEST 我先查记录。"}]}),
                     _line("assistant", "a2", at(2), message={"role": "assistant", "model": "TEST-model",
                                                              "content": [{"type": "tool_use", "id": "T1", "name": "Bash",
                                                                           "input": {"command": "ls"}}]}))
    stop = {"hook_event_name": "Stop", "session_id": "TEST-work-session", "prompt_id": "TEST-p1",
            "transcript_path": str(record), "cwd": "C:/work", "last_assistant_message": "TEST QX-17 已完成。"}
    _hook(config, stop)
    _hook(config, stop)
    said = sorted((role, content) for role, _origin, content, _at in _rows(root, "workpc-claude-code"))
    assert said == sorted([("user", "TEST 帮我看一下 QX-17。"), ("assistant", "TEST 我先查记录。"),
                           ("assistant", "TEST QX-17 已完成。")])
    cursor = transcript.Cursor(config["state_dir"], "TEST-work-session", record)
    assert cursor.load() == record.stat().st_size, "the cursor moves as far as the server stored"
    entries = {entry["entry_id"]: entry["display_name"] for entry in read_shared_payload(root)["entries"]}
    assert entries["workpc-claude-code"] == "工作机 Claude Code"


def test_codex_keeps_what_it_could_not_send_and_sends_it_with_its_moment(served, tmp_path, monkeypatch):
    root, _homes, ports = served
    flushes = []
    monkeypatch.setattr(remote_client, "_start_flush", lambda config: flushes.append(config))
    offline = _client(tmp_path, "codex", _free_port())
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-codex-session", "turn_id": "TEST-t1",
              "prompt": "TEST 记住 KZ-42 的截止日期是周五。", "cwd": "C:/work"}
    assert _hook(offline, prompt) == {}
    spooled = list((offline["state_dir"] / "spool").glob("*.json"))
    assert len(spooled) == 1
    kept_at = json.loads(spooled[0].read_text(encoding="utf-8"))["observed_at"]
    (offline["state_dir"] / "server-away").unlink()  # a minute later, the server back
    online = dict(offline, url=f"http://127.0.0.1:{ports['codex']}")
    _hook(online, {"hook_event_name": "Stop", "session_id": "TEST-codex-session", "turn_id": "TEST-t1",
                   "last_assistant_message": "TEST 记下了。", "cwd": "C:/work"})
    assert len(flushes) == 1, "a hook that got through starts the flush in a process of its own"
    assert remote_client.flush_spool(online, 20) == 1
    rows = _rows(root, "workpc-codex")
    assert sorted(content for _role, _origin, content, _at in rows) == ["TEST 记下了。", "TEST 记住 KZ-42 的截止日期是周五。"]
    assert next(at for _role, _origin, content, at in rows if content.startswith("TEST 记住")) == kept_at
    assert list((offline["state_dir"] / "spool").glob("*.json")) == []
    _hook(online, prompt)
    assert len(_rows(root, "workpc-codex")) == 2, "the same hook sent again is the same source"


def test_the_client_goes_to_the_server_itself_past_a_proxy(served, tmp_path, monkeypatch):
    """A client machine's proxy (HTTP_PROXY on 127.0.0.1) is for the internet and cannot reach a private address."""
    root, _homes, ports = served
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    config = _client(tmp_path, "claude-code", ports["claude-code"])
    _hook(config, {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-work-session", "prompt_id": "TEST-p9",
                   "prompt": "TEST 代理不该挡住这句。", "cwd": "C:/work"})
    assert [content for _role, _origin, content, _at in _rows(root, "workpc-claude-code")] == ["TEST 代理不该挡住这句。"]


def test_a_server_that_is_away_holds_hooks_up_once_a_minute(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", _free_port())
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-codex-session", "turn_id": "TEST-t1",
              "prompt": "TEST 服务器不在。", "cwd": "C:/work"}
    assert _hook(config, prompt) == {}
    marker = config["state_dir"] / "server-away"
    assert marker.is_file()
    tried = []
    monkeypatch.setattr(remote_client, "_post", lambda *args: tried.append(args) or None)
    assert _hook(config, dict(prompt, turn_id="TEST-t2")) == {}
    assert tried == [], "a hook does not try while the server was away less than a minute ago"
    assert len(list((config["state_dir"] / "spool").glob("*.json"))) == 2, "both are kept to be sent later"
    old = time.time() - remote_client.AWAY_SECONDS - 1
    os.utime(marker, (old, old))
    _hook(config, dict(prompt, turn_id="TEST-t3"))
    assert len(tried) == 1, "a minute later a hook tries again"
    log = (config["state_dir"] / "remote-client.log").read_text(encoding="utf-8")
    assert "UserPromptSubmit: no connection" in log and "UserPromptSubmit: not sent" in log


@pytest.mark.skipif(os.name != "nt", reason="console windows are a Windows matter")
def test_the_flush_process_opens_no_console_window(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(remote_client.subprocess, "Popen", lambda argv, **options: started.append(options))
    remote_client._start_flush(_client(tmp_path, "codex", 18766))
    flags = started[0]["creationflags"]
    assert flags & remote_client.subprocess.CREATE_NO_WINDOW, "a console without a window, for a launcher's child"
    assert not flags & remote_client.subprocess.DETACHED_PROCESS, "a detached launcher's python.exe gets a window"


def test_the_server_logs_each_hook_and_each_refused_request(served, tmp_path):
    _root, homes, ports = served
    root_logger = logging.getLogger()
    level = root_logger.level
    handler = remote_server.log_to_file(homes["claude-code"])
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "TEST-work-session", "prompt_id": "TEST-p7",
              "prompt": "TEST 记一笔。", "cwd": "C:/work"}
    try:
        _hook(_client(tmp_path, "claude-code", ports["claude-code"]), prompt)
        (tmp_path / "TEST-other").mkdir()
        wrong = _client(tmp_path / "TEST-other", "claude-code", ports["claude-code"], token="TEST-not-the-token")
        _hook(wrong, prompt)
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(level)
        handler.close()
    log = (homes["claude-code"] / "scope-recall" / remote_server.LOG_NAME).read_text(encoding="utf-8")
    assert "hook UserPromptSubmit: " in log
    assert "refused POST /hook from 127.0.0.1: no valid token" in log
    assert "UserPromptSubmit: HTTP 401" in (wrong["state_dir"] / "remote-client.log").read_text(encoding="utf-8")


def test_the_entry_s_mcp_tools_answer_over_http_behind_the_token(served):
    _root, _homes, ports = served
    url = f"http://127.0.0.1:{ports['claude-code']}/mcp"
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "TEST-client", "version": "0"}}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(initialize).encode(), headers=headers,
                                                      method="POST"), timeout=10)
    assert refused.value.code == 401
    headers["Authorization"] = f"Bearer {TOKEN}-claude-code"

    def call(message):
        request = urllib.request.Request(url, data=json.dumps(message).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8")
        data = [line[len("data:"):].strip() for line in text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1] if data else text)

    assert call(initialize)["result"]["serverInfo"]["name"]
    tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["result"]["tools"]
    assert {"recall", "status"} <= {tool["name"] for tool in tools}


def test_record_lines_from_the_wire_are_checked():
    good = {"entry_id": "u1", "role": "user", "text": "TEST 好", "occurred_at": "2026-09-27T06:00:00Z", "prompt_id": "p"}
    record = remote_server.record_from_wire({"start": 10, "lines": [[20, good], [30, {"role": "tool"}], [40, None]]})
    assert [end for end, _said in record.lines] == [20, 30, 40]
    assert record.lines[0][1] == transcript.Said("u1", "user", "TEST 好", "2026-09-27T06:00:00Z", "p")
    assert record.lines[1][1] is None, "what the record reader would not produce is nothing said"
    for bad in ({"start": 10, "lines": [[10, good]]}, {"start": -1, "lines": []}, {"lines": []}, [1]):
        with pytest.raises(remote_server.RemoteServerError):
            remote_server.record_from_wire(bad)
    assert transcript.said_from_wire(dict(good, role="assistant")) is None, "only a person's message has a prompt id"


def test_the_listen_address_is_one_private_interface(store):
    _root, homes = store
    for listen in ("0.0.0.0", "::", "not-an-address"):
        with pytest.raises(remote_server.RemoteServerError):
            remote_server.write_server_config(homes["codex"], "codex", listen=listen, port=18765, token_sha256="0" * 64)
    assert remote_server.token_matches(f"Bearer {TOKEN}", hashlib.sha256(TOKEN.encode()).hexdigest())
    assert not remote_server.token_matches(f"Bearer {TOKEN}x", hashlib.sha256(TOKEN.encode()).hexdigest())
    assert not remote_server.token_matches(None, hashlib.sha256(TOKEN.encode()).hexdigest())


def test_a_remote_client_waits_as_long_as_a_local_one():
    """The remote plugin's hooks wait what the local installers' do, for the events it forwards, and Codex's
    SessionEnd and Interrupt stay within the 3 s Codex allows them."""
    from scope_recall.maintenance import install_claude_code, install_codex

    assert remote_client.HOOK_TIMEOUTS["claude-code"] == install_claude_code.HOOK_TIMEOUTS
    codex = remote_client.HOOK_TIMEOUTS["codex"]
    assert codex == {event: install_codex.HOOK_TIMEOUTS[event] for event in codex}
    assert codex["UserPromptSubmit"] == install_claude_code.HOOK_TIMEOUTS["UserPromptSubmit"]
    assert max(install_codex.HOOK_TIMEOUTS["SessionEnd"], install_codex.HOOK_TIMEOUTS["Interrupt"]) <= 3


@pytest.mark.parametrize("host", remote_client.HOSTS)
def test_the_plugin_sends_hooks_and_tools_to_the_server(tmp_path, host):
    config = _client(tmp_path, host, 18765)
    files = remote_client.plugin_files(config, tmp_path / "TEST-plugin" / "scope-recall")
    by_name = {path.relative_to(tmp_path / "TEST-plugin" / "scope-recall").as_posix(): text for path, text in files.items()}
    mcp = json.loads(by_name[".mcp.json"])["mcpServers"]["scope-recall"]
    assert mcp["url"] == "http://127.0.0.1:18765/mcp"
    headers = mcp["headers"] if host == "claude-code" else mcp["http_headers"]
    assert headers == {"Authorization": f"Bearer {TOKEN}-{host}"}
    hooks = json.loads(by_name["hooks/hooks.json"])["hooks"]
    assert set(hooks) == set(remote_client.HOOK_TIMEOUTS[host])
    command = hooks["Stop"][0]["hooks"][0]["command"]
    assert "scope_recall.adapters.codex.remote_client" in command and "--config" in command
    assert "skills/scope-recall-memory/SKILL.md" in by_name
