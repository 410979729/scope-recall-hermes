"""A client on another machine: its hooks and MCP tools reach its entry's server over HTTP.

The server runs in this process on 127.0.0.1 with a real shared store; the client posts to it as the hook on the
other machine would.  Sources are synthetic; nothing here is a person's memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from scope_recall.adapters.clients import remote_client, remote_server, transcript
from scope_recall.adapters.codex import remote_client as remote_entry
from scope_recall.adapters.hermes.installation import (
    attach_shared_entry,
    attach_shared_record,
    build_installation_manifest,
    client_entry_record,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)
from scope_recall.maintenance import install_remote

NOW = "2026-09-27T06:00:00Z"
AGENT = "TEST-agent"
TOKEN = "TEST-token-0123456789-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "TEST-shared"
    write_shared_payload(root, new_shared_payload(root, agent_id=AGENT))
    home = tmp_path / "TEST-tianshu-home"
    home.mkdir()
    attach_shared_entry(
        root,
        build_installation_manifest(home, agent_id=AGENT, user_id="TEST-owner", agent_workspace="TEST-workspace"),
        entry_id="tianshu",
        display_name="天枢",
        now=NOW,
    )
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    homes = {}
    for host, entry, name in (
        ("claude-code", "workpc-claude-code", "工作机 Claude Code"),
        ("codex", "workpc-codex", "工作机 Codex"),
    ):
        client = tmp_path / f"TEST-{entry}-home"
        attach_shared_record(
            root,
            client_entry_record(
                host=host,
                home=client,
                entry_id=entry,
                display_name=name,
                attached_at=NOW,
                allowed_scope_ids=owner["allowed_scope_ids"],
                writable_scope_ids=owner["writable_scope_ids"],
                capture_scope_id=owner["capture_scope_id"],
            ),
            now=NOW,
        )
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
        remote_server.write_server_config(
            home,
            host,
            listen="127.0.0.1",
            port=port,
            token_sha256=hashlib.sha256(f"{TOKEN}-{host}".encode()).hexdigest(),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                remote_server.build_app(remote_server.load_server_config(home, host)),
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
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
    config.write_text(
        json.dumps(
            {
                "url": f"http://127.0.0.1:{port}",
                "host": host,
                "token_file": str(token_file),
                "state_dir": str(state / "state"),
            }
        ),
        encoding="utf-8",
    )
    return remote_client.load_client_config(config)


def _rows(root: Path, entry: str) -> list[tuple]:
    with closing(sqlite3.connect(root / "memory.sqlite3")) as connection:
        return connection.execute(
            "SELECT role, origin, content, occurred_at FROM source_events WHERE entry_id=? ORDER BY content", (entry,)
        ).fetchall()


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
    assert (
        _hook(
            config,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-work-session",
                "prompt_id": "TEST-p1",
                "prompt": "TEST 这句不该进库",
                "cwd": "C:/work",
            },
        )
        == {}
    )
    assert _rows(root, "workpc-claude-code") == []
    request = urllib.request.Request(f"http://127.0.0.1:{ports['claude-code']}/health")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 401


def test_claude_code_prompt_and_record_reach_the_entry_once(served, tmp_path):
    root, _homes, ports = served
    config = _client(tmp_path, "claude-code", ports["claude-code"])
    at = _moments()
    _hook(
        config,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-work-session",
            "prompt_id": "TEST-p1",
            "prompt": "TEST 帮我看一下 QX-17。",
            "cwd": "C:/work",
        },
    )
    record = _record(
        tmp_path / "TEST-work-projects" / "TEST-work-session.jsonl",
        _line(
            "user",
            "u1",
            at(0),
            origin={"kind": "human"},
            promptId="TEST-p1",
            message={"role": "user", "content": "TEST 帮我看一下 QX-17。"},
        ),
        _line(
            "assistant",
            "a1",
            at(1),
            message={
                "role": "assistant",
                "model": "TEST-model",
                "content": [{"type": "text", "text": "TEST 我先查记录。"}],
            },
        ),
        _line(
            "assistant",
            "a2",
            at(2),
            message={
                "role": "assistant",
                "model": "TEST-model",
                "content": [{"type": "tool_use", "id": "T1", "name": "Bash", "input": {"command": "ls"}}],
            },
        ),
    )
    stop = {
        "hook_event_name": "Stop",
        "session_id": "TEST-work-session",
        "prompt_id": "TEST-p1",
        "transcript_path": str(record),
        "cwd": "C:/work",
        "last_assistant_message": "TEST QX-17 已完成。",
    }
    _hook(config, stop)
    _hook(config, stop)
    said = sorted((role, content) for role, _origin, content, _at in _rows(root, "workpc-claude-code"))
    assert said == sorted(
        [("user", "TEST 帮我看一下 QX-17。"), ("assistant", "TEST 我先查记录。"), ("assistant", "TEST QX-17 已完成。")]
    )
    cursor = transcript.Cursor(config["state_dir"], "TEST-work-session", record)
    assert cursor.load() == record.stat().st_size, "the cursor moves as far as the server stored"
    entries = {entry["entry_id"]: entry["display_name"] for entry in read_shared_payload(root)["entries"]}
    assert entries["workpc-claude-code"] == "工作机 Claude Code"


def test_codex_keeps_what_it_could_not_send_and_sends_it_with_its_moment(served, tmp_path, monkeypatch):
    root, _homes, ports = served
    flushes = []
    monkeypatch.setattr(remote_client, "_start_flush", lambda config: flushes.append(config))
    offline = _client(tmp_path, "codex", _free_port())
    prompt = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "TEST-codex-session",
        "turn_id": "TEST-t1",
        "prompt": "TEST 记住 KZ-42 的截止日期是周五。",
        "cwd": "C:/work",
    }
    assert _hook(offline, prompt) == {}
    spooled = list((offline["state_dir"] / "spool").glob("*.json"))
    assert len(spooled) == 1
    kept_at = json.loads(spooled[0].read_text(encoding="utf-8"))["observed_at"]
    (offline["state_dir"] / "server-away").unlink()  # a minute later, the server back
    online = dict(offline, url=f"http://127.0.0.1:{ports['codex']}")
    _hook(
        online,
        {
            "hook_event_name": "Stop",
            "session_id": "TEST-codex-session",
            "turn_id": "TEST-t1",
            "last_assistant_message": "TEST 记下了。",
            "cwd": "C:/work",
        },
    )
    assert len(flushes) == 1, "a hook that got through starts the flush in a process of its own"
    assert remote_client.flush_spool(online, 20) == 1
    rows = _rows(root, "workpc-codex")
    assert sorted(content for _role, _origin, content, _at in rows) == [
        "TEST 记下了。",
        "TEST 记住 KZ-42 的截止日期是周五。",
    ]
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
    _hook(
        config,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-work-session",
            "prompt_id": "TEST-p9",
            "prompt": "TEST 代理不该挡住这句。",
            "cwd": "C:/work",
        },
    )
    assert [content for _role, _origin, content, _at in _rows(root, "workpc-claude-code")] == [
        "TEST 代理不该挡住这句。"
    ]


def test_a_server_that_is_away_holds_hooks_up_once_a_minute(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", _free_port())
    prompt = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "TEST-codex-session",
        "turn_id": "TEST-t1",
        "prompt": "TEST 服务器不在。",
        "cwd": "C:/work",
    }
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
    prompt = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "TEST-work-session",
        "prompt_id": "TEST-p7",
        "prompt": "TEST 记一笔。",
        "cwd": "C:/work",
    }
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
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "TEST-client", "version": "0"},
        },
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(
            urllib.request.Request(url, data=json.dumps(initialize).encode(), headers=headers, method="POST"),
            timeout=10,
        )
    assert refused.value.code == 401
    headers["Authorization"] = f"Bearer {TOKEN}-claude-code"

    def call(message):
        request = urllib.request.Request(url, data=json.dumps(message).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8")
        data = [line[len("data:") :].strip() for line in text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1] if data else text)

    assert call(initialize)["result"]["serverInfo"]["name"]
    tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["result"]["tools"]
    assert {"recall", "status"} <= {tool["name"] for tool in tools}


def test_record_lines_from_the_wire_are_checked():
    good = {
        "entry_id": "u1",
        "role": "user",
        "text": "TEST 好",
        "occurred_at": "2026-09-27T06:00:00Z",
        "prompt_id": "p",
    }
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
    assert remote_client.HOOK_TIMEOUTS["workbuddy"] == remote_client.HOOK_TIMEOUTS["claude-code"]


@pytest.mark.parametrize("host", remote_client.HOSTS)
def test_the_plugin_sends_hooks_and_tools_to_the_server(tmp_path, host):
    config = _client(tmp_path, host, 18765)
    if host == "workbuddy":
        # WorkBuddy has no plugin: install merges into its own settings (the next test), and never writes a Codex one.
        with pytest.raises(remote_client.RemoteClientError, match="no plugin"):
            install_remote.plugin_files(config, tmp_path / "TEST-plugin" / "scope-recall")
        return
    files = install_remote.plugin_files(config, tmp_path / "TEST-plugin" / "scope-recall")
    by_name = {
        path.relative_to(tmp_path / "TEST-plugin" / "scope-recall").as_posix(): text for path, text in files.items()
    }
    mcp = json.loads(by_name[".mcp.json"])["mcpServers"]["scope-recall"]
    assert mcp["url"] == "http://127.0.0.1:18765/mcp"
    headers = mcp["headers"] if host == "claude-code" else mcp["http_headers"]
    assert headers == {"Authorization": f"Bearer {TOKEN}-{host}"}
    hooks = json.loads(by_name["hooks/hooks.json"])["hooks"]
    assert set(hooks) == set(remote_client.HOOK_TIMEOUTS[host])
    command = hooks["Stop"][0]["hooks"][0]["command"]
    assert "scope_recall.adapters.codex.remote_client" in command and "--config" in command
    assert "skills/scope-recall-memory/SKILL.md" in by_name


def test_a_workbuddy_client_merges_its_hooks_and_server_into_workbuddy_s_own_files(tmp_path, capsys):
    """WorkBuddy reads hooks and MCP servers from its own home, beside its own keys and another tool's hook: install
    adds this client's there, keeps a copy of each file it changes, changes nothing when run again, takes a new token
    where the server stands, and refuses to run beside another Scope Recall hook."""
    import shlex

    from scope_recall.maintenance import install_workbuddy

    config = _client(tmp_path, "workbuddy", 18767)
    home = tmp_path / "TEST-profile" / ".workbuddy"
    home.mkdir(parents=True)
    settings = {
        "sandbox": {"enabled": True},
        "enabledPlugins": {"TEST@TEST": True},
        "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "TEST-other-tool"}]}]},
    }
    mcp = {"mcpServers": {"TEST-other-server": {"type": "http", "url": "http://127.0.0.1:9/TEST"}}}
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (home / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    # WorkBuddy's own record of its connector proxy, which its agent is started with alone.
    (home / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"connector-proxy": {"url": "http://127.0.0.1:9/mcp"}}}), encoding="utf-8"
    )
    before = {name: (home / name).read_bytes() for name in ("settings.json", "mcp.json")}
    proxy = (home / ".mcp.json").read_bytes()

    assert remote_entry.main(["install", "--config", str(config["config"]), "--plugin-dir", str(home)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert sorted(Path(path).name for path in result["written"]) == ["mcp.json", "settings.json"]
    assert {Path(path).name: Path(path).read_bytes() for path in result["backups"]} == before
    assert all(Path(path).is_relative_to(config["state_dir"] / "backups") for path in result["backups"])
    written = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert {key: value for key, value in written.items() if key != "hooks"} == {
        key: value for key, value in settings.items() if key != "hooks"
    }
    assert written["hooks"]["Stop"][0] == settings["hooks"]["Stop"][0], "another tool's hook stays first"
    command = written["hooks"]["Stop"][-1]["hooks"][0]["command"]
    assert shlex.split(command) == [*install_remote._hook_argv(config), "||", "exit", "1"], "a failure never blocks"
    assert command.startswith('"') and "\\" not in command, "Git Bash runs it: quoted, forward slashes"
    assert {
        event: groups[-1]["hooks"][0]["timeout"] for event, groups in written["hooks"].items()
    } == remote_client.HOOK_TIMEOUTS["workbuddy"]
    servers = json.loads((home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == ["TEST-other-server", "scope-recall"]
    assert servers["scope-recall"] == {
        "type": "http",
        "url": "http://127.0.0.1:18767/mcp",
        "headers": {"Authorization": f"Bearer {TOKEN}-workbuddy"},
        "description": install_workbuddy.SERVER_DESCRIPTION,
    }

    assert install_remote.install(config, home) == {"written": [], "backups": []}, "run again, nothing changes"
    config["token_file"].write_text("TEST-token-of-a-new-machine", encoding="utf-8")
    assert [Path(path).name for path in install_remote.install(config, home)["written"]] == ["mcp.json"]
    servers = json.loads((home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == ["TEST-other-server", "scope-recall"]
    assert servers["scope-recall"]["headers"] == {"Authorization": "Bearer TEST-token-of-a-new-machine"}

    local = (
        '"C:/TEST/python.exe" -I -B -m scope_recall.adapters.codex.hook_entry --home "C:/TEST-entry" --host workbuddy'
    )
    written["hooks"]["UserPromptSubmit"] = [{"hooks": [{"type": "command", "command": local}]}]
    (home / "settings.json").write_text(json.dumps(written), encoding="utf-8")
    held = (home / "settings.json").read_bytes()
    with pytest.raises(remote_client.RemoteClientError, match="another Scope Recall hook"):
        install_remote.install(config, home)
    assert (home / "settings.json").read_bytes() == held
    with pytest.raises(remote_client.RemoteClientError, match="does not exist"):
        install_remote.install(config, tmp_path / "TEST-nowhere")
    assert (home / ".mcp.json").read_bytes() == proxy, "WorkBuddy's own proxy record is not touched"


def test_the_hook_answers_in_ascii_whatever_the_code_page(tmp_path, monkeypatch, capsys):
    """A pipe on Windows carries the system code page (GBK on a Chinese Windows, under -I whatever PYTHONUTF8
    says), and the host reads UTF-8: recalled Chinese text written as it is arrived garbled or not at all."""
    import io

    config = _client(tmp_path, "claude-code", _free_port())
    recalled = {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "TEST 天枢记得 QX-17。"}
    }
    monkeypatch.setattr(remote_client, "run_hook", lambda *args, **kwargs: recalled)
    monkeypatch.setattr(remote_client.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(b"{}")})())
    assert remote_client.main(["--config", str(config["config"])]) == 0
    out = capsys.readouterr().out
    assert out.isascii() and json.loads(out) == recalled


def test_a_workbuddy_client_with_nothing_to_add_writes_nothing(tmp_path, monkeypatch, capsys):
    """WorkBuddy puts a prompt hook's whole stdout in front of the prompt unless it carries additionalContext."""
    import io

    monkeypatch.setattr(remote_client, "run_hook", lambda *args, **kwargs: {})
    for host, expected in (("workbuddy", ""), ("claude-code", "{}\n")):
        config = _client(tmp_path, host, _free_port())
        monkeypatch.setattr(remote_client.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(b"{}")})())
        assert remote_client.main(["--config", str(config["config"])]) == 0
        assert capsys.readouterr().out == expected, host


def test_a_workbuddy_client_whose_config_does_not_load_writes_nothing(tmp_path, monkeypatch, capsys):
    """A hook whose client.json no longer loads still answers with nothing to add as the host it names takes it."""
    import io

    for host, expected in (("workbuddy", ""), ("claude-code", "{}\n")):
        path = _client(tmp_path, host, _free_port())["config"]
        path.write_text(
            json.dumps({**json.loads(path.read_text(encoding="utf-8")), "state_dir": "TEST-relative"}), encoding="utf-8"
        )
        monkeypatch.setattr(remote_client.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(b"{}")})())
        assert remote_client.main(["--config", str(path)]) == 0
        captured = capsys.readouterr()
        assert (captured.out, "SCOPE_RECALL_REMOTE:" in captured.err) == (expected, True), host


def test_the_server_opens_no_path_a_request_names(served, tmp_path):
    """A Stop that sent no record lines had nothing new to send.  The payload's transcript_path names a file on the
    client's machine; sent on purpose it could name one here, which the server read as this entry's messages."""
    root, homes, _ports = served
    here = _record(
        tmp_path / "TEST-this-machine" / "TEST-work-session.jsonl",
        _line(
            "user",
            "u9",
            _moments()(0),
            origin={"kind": "human"},
            promptId="TEST-p9",
            message={"role": "user", "content": "TEST 这台机器上的记录"},
        ),
    )
    config = remote_server.load_server_config(homes["claude-code"], "claude-code")
    answer = remote_server.handle_request(
        config,
        {
            "payload": {
                "hook_event_name": "Stop",
                "session_id": "TEST-work-session",
                "prompt_id": "TEST-p9",
                "cwd": "C:/work",
                "transcript_path": str(here),
                "last_assistant_message": "TEST 好的。",
            }
        },
    )
    assert answer["through"] is None
    said = [content for _role, _origin, content, _at in _rows(root, "workpc-claude-code")]
    assert "TEST 这台机器上的记录" not in said and "TEST 好的。" in said


def test_a_failed_capture_s_code_reaches_the_server_log(served, tmp_path, monkeypatch):
    _root, homes, ports = served
    monkeypatch.setattr(
        remote_server,
        "handle_request",
        lambda config, body, started=None, recaller=None: {
            "result": {},
            "through": None,
            "reason": "capture_failed",
            "error": "DEADLINE_EXCEEDED",
        },
    )
    root_logger = logging.getLogger()
    level = root_logger.level
    handler = remote_server.log_to_file(homes["codex"])
    try:
        _hook(
            _client(tmp_path, "codex", ports["codex"]),
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-t1",
                "prompt": "TEST 记一笔。",
                "cwd": "C:/work",
            },
        )
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(level)
        handler.close()
    log = (homes["codex"] / "scope-recall" / remote_server.LOG_NAME).read_text(encoding="utf-8")
    assert "hook UserPromptSubmit: capture_failed (DEADLINE_EXCEEDED), record through None" in log


def test_a_failed_recall_s_cause_reaches_the_server_log(served, tmp_path, monkeypatch):
    """The work computer's Codex server logged recall_exception three times and nothing else."""
    _root, homes, ports = served
    monkeypatch.setattr(
        remote_server,
        "handle_request",
        lambda config, body, started=None, recaller=None: {
            "result": {},
            "through": None,
            "reason": "recall_exception",
            "error": None,
            "recall_error": "ContractError:INPUT_INVALID",
        },
    )
    root_logger = logging.getLogger()
    level = root_logger.level
    handler = remote_server.log_to_file(homes["codex"])
    try:
        _hook(
            _client(tmp_path, "codex", ports["codex"]),
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-t2",
                "prompt": "TEST 问一句。",
                "cwd": "C:/work",
            },
        )
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(level)
        handler.close()
    log = (homes["codex"] / "scope-recall" / remote_server.LOG_NAME).read_text(encoding="utf-8")
    assert "hook UserPromptSubmit: recall_exception (ContractError:INPUT_INVALID), record through None" in log


def test_a_server_that_cannot_start_a_vector_helper_ahead_still_serves(store, monkeypatch):
    """Keeping a helper ready is a speed-up: failing to start one must not keep the entry from being served."""
    import sys as system
    import types

    from scope_recall.vector import process_store

    root, homes = store
    remote_server.write_server_config(
        homes["codex"], "codex", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    config = remote_server.load_server_config(homes["codex"], "codex")
    served = []
    monkeypatch.setattr(system, "platform", "win32")
    monkeypatch.setitem(system.modules, "uvicorn", types.SimpleNamespace(run=lambda app, **kwargs: served.append(app)))
    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: (_ for _ in ()).throw(OSError("TEST no helper")))
    root_logger = logging.getLogger()
    level = root_logger.level
    try:
        remote_server.serve(config)
    finally:
        for handler in list(root_logger.handlers):
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(level)
    assert len(served) == 1
    log = (homes["codex"] / "scope-recall" / remote_server.LOG_NAME).read_text(encoding="utf-8")
    assert "could not start a vector helper ahead: OSError" in log


def test_the_server_warms_its_kept_handler_when_it_starts(store, monkeypatch):
    """Made at the first prompt, the kept handler opened the table inside that prompt's recall: the first two prompts
    after the 3.4.0 restart recalled by words alone (2026-09-29 13:54:57 and 13:55:34)."""
    import sys as system
    import types

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    root, homes = store
    remote_server.write_server_config(
        homes["codex"], "codex", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    config = remote_server.load_server_config(homes["codex"], "codex")
    warmed = []
    monkeypatch.setitem(system.modules, "uvicorn", types.SimpleNamespace(run=lambda app, **kwargs: None))
    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.setattr(local_endpoint.KeptRecaller, "warm", lambda self, *args: warmed.append(self))
    root_logger = logging.getLogger()
    level = root_logger.level
    try:
        remote_server.serve(config)
        remote_server.build_app(config)
    finally:
        for handler in list(root_logger.handlers):
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(level)
    assert len(warmed) == 1, "serve warms; a test's app does not"


def test_the_server_shares_one_vector_store_among_its_handlers(store, monkeypatch):
    """A prompt that came while the kept handler was busy got a handler whose store started a LanceDB helper of its
    own: parallel sub-agents on the work computer lost their vector search to that helper's start (2026-09-30).  The
    server shares its stores before anything builds one."""
    import sys as system
    import types

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    root, homes = store
    remote_server.write_server_config(
        homes["codex"], "codex", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    config = remote_server.load_server_config(homes["codex"], "codex")
    seen = []
    monkeypatch.setattr(system, "platform", "win32")
    monkeypatch.setitem(system.modules, "uvicorn", types.SimpleNamespace(run=lambda app, **kwargs: None))
    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: seen.append(process_store._sharing))
    monkeypatch.setattr(local_endpoint.KeptRecaller, "warm", lambda self, *args: seen.append(process_store._sharing))
    root_logger = logging.getLogger()
    level = root_logger.level
    try:
        remote_server.serve(config)
    finally:
        for handler in list(root_logger.handlers):
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(level)
    assert seen == [True, True], "sharing before the spare helper and before the kept handler is warmed"


def test_a_recall_without_its_vector_search_is_named_in_the_server_log(served, tmp_path, monkeypatch):
    """The work computer's recalls ran without their vector search for as long as anyone could tell: the packet
    carried the gap to the model, and the server's log said nothing."""
    _root, homes, ports = served
    monkeypatch.setattr(
        remote_server,
        "handle_request",
        lambda config, body, started=None, recaller=None: {
            "result": {},
            "through": None,
            "reason": None,
            "error": None,
            "recall_error": None,
            "recall_vector": "vector_error:TimeoutError:helper_open_deadline",
        },
    )
    root_logger = logging.getLogger()
    level = root_logger.level
    handler = remote_server.log_to_file(homes["codex"])
    try:
        _hook(
            _client(tmp_path, "codex", ports["codex"]),
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-t3",
                "prompt": "TEST 再问一句。",
                "cwd": "C:/work",
            },
        )
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(level)
        handler.close()
    log = (homes["codex"] / "scope-recall" / remote_server.LOG_NAME).read_text(encoding="utf-8")
    assert (
        "hook UserPromptSubmit: None (recall without vectors: vector_error:TimeoutError:helper_open_deadline), "
        "record through None"
    ) in log


def test_a_plugin_command_survives_the_shell_that_runs_it(tmp_path):
    """Claude Code runs a hook through a shell: a path it would split is refused, as the local installer refuses
    it.  Codex's POSIX command is quoted as shell words, a quote in a path included."""
    import shlex

    (tmp_path / "TEST with space").mkdir()
    spaced = _client(tmp_path / "TEST with space", "claude-code", 18765)
    with pytest.raises(remote_client.RemoteClientError):
        install_remote.plugin_files(spaced, tmp_path / "TEST-plugin" / "scope-recall")
    (tmp_path / "TEST-o'brien").mkdir()
    quoted = _client(tmp_path / "TEST-o'brien", "codex", 18766)
    files = install_remote.plugin_files(quoted, tmp_path / "TEST-codex-plugin" / "scope-recall-codex")
    hooks = json.loads(next(text for path, text in files.items() if path.name == "hooks.json"))["hooks"]
    assert shlex.split(hooks["Stop"][0]["hooks"][0]["command"]) == install_remote._hook_argv(quoted)


def test_a_missing_token_file_answers_nothing_and_says_why(tmp_path):
    config = _client(tmp_path, "claude-code", _free_port())
    config["token_file"].unlink()
    assert (
        _hook(
            config,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-work-session",
                "prompt_id": "TEST-p1",
                "prompt": "TEST 没有令牌。",
                "cwd": "C:/work",
            },
        )
        == {}
    )
    assert "UserPromptSubmit: no token to send" in (config["state_dir"] / "remote-client.log").read_text(
        encoding="utf-8"
    )


def test_a_full_spool_drops_its_oldest_and_says_so(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", _free_port())
    monkeypatch.setattr(remote_client, "SPOOL_LIMIT", 2)
    for turn in range(3):
        remote_client._spool(config, {"hook_event_name": "Stop", "turn_id": f"TEST-t{turn}"}, NOW)
    assert len(list((config["state_dir"] / "spool").glob("*.json"))) == 2
    assert "spool full: dropped the 1 oldest" in (config["state_dir"] / "remote-client.log").read_text(encoding="utf-8")


def _big_prompt(turn: str) -> dict:
    """A prompt the server refuses for good: it measures the payload as it serializes it, and this is past that."""
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "TEST-codex-session",
        "turn_id": turn,
        "prompt": "TEST " + "x" * remote_server.MAX_PAYLOAD_BYTES,
        "cwd": "C:/work",
    }


def test_a_request_refused_for_good_neither_stays_nor_stops_the_spool(served, tmp_path, monkeypatch):
    """A 400 or 413 is the request itself, refused again whenever it is sent.  Kept at the head of the spool it
    stopped every later flush, until 256 newer ones pushed it out; and a live hook that got one was kept too."""
    root, _homes, ports = served
    monkeypatch.setattr(remote_client, "_start_flush", lambda config: None)
    config = _client(tmp_path, "codex", ports["codex"])
    folder = config["state_dir"] / "spool"
    folder.mkdir(parents=True)
    (folder / "00000000000000000001-1.json").write_text(
        json.dumps({"payload": _big_prompt("TEST-big-1"), "observed_at": NOW}), encoding="utf-8"
    )
    remote_client._spool(
        config,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-codex-session",
            "turn_id": "TEST-t2",
            "prompt": "TEST 后面这句要存下来。",
            "cwd": "C:/work",
        },
        NOW,
    )
    assert remote_client.flush_spool(config, 20) == 1
    assert list(folder.glob("*.json")) == []
    assert [row[2] for row in _rows(root, "workpc-codex")] == ["TEST 后面这句要存下来。"]
    assert _hook(config, _big_prompt("TEST-big-2")) == {}
    assert list(folder.glob("*.json")) == [], "a live hook refused for good is not kept either"
    assert "refused for good: not kept" in (config["state_dir"] / "remote-client.log").read_text(encoding="utf-8")


def test_a_client_clock_ahead_neither_hides_its_messages_nor_holds_back_their_work(store):
    """A recall finds nothing dated after its now, and the time a hook carried set when its work fell due: a message
    dated a day ahead by a fast client clock was found by no recall for a day and embedded a day late.  A time more
    than a minute ahead is this machine's now; within the minute it is kept as sent, so a correct client's hook sent
    again from the spool is the same source.  When a message was stored and its work are this machine's time."""
    root, homes = store
    remote_server.write_server_config(
        homes["codex"], "codex", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    config = remote_server.load_server_config(homes["codex"], "codex")

    def parse(at):
        return datetime.fromisoformat(at.replace("Z", "+00:00"))

    ahead = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    remote_server.handle_request(
        config,
        {
            "payload": {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-ahead",
                "prompt": "TEST 时钟快了一天。",
                "cwd": "C:/work",
            },
            "observed_at": ahead,
        },
    )
    latest = datetime.now(timezone.utc) + timedelta(seconds=remote_server.CLOCK_AHEAD_SECONDS)
    stored = next(at for _role, _origin, content, at in _rows(root, "workpc-codex") if content == "TEST 时钟快了一天。")
    assert parse(stored) <= latest, stored
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        persisted = db.execute(
            "SELECT persisted_at FROM source_events WHERE content=?", ("TEST 时钟快了一天。",)
        ).fetchone()[0]
        due = [
            row[0]
            for row in db.execute(
                "SELECT w.available_at FROM work_items w JOIN source_events e ON w.subject_ref=e.event_id "
                "AND w.subject_revision=e.source_revision WHERE e.content=?",
                ("TEST 时钟快了一天。",),
            )
        ]
    assert parse(persisted) <= latest
    assert due and all(parse(at) <= latest for at in due), due

    right = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    body = {
        "payload": {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-codex-session",
            "turn_id": "TEST-right",
            "prompt": "TEST 时钟是对的。",
            "cwd": "C:/work",
        },
        "observed_at": right,
    }
    remote_server.handle_request(config, body)
    remote_server.handle_request(config, body)
    rows = [row for row in _rows(root, "workpc-codex") if row[2] == "TEST 时钟是对的。"]
    assert len(rows) == 1 and rows[0][3] == right, rows

    said = transcript.Said("TEST-e1", "user", "TEST 记录里的一句。", ahead)
    _observed, lines = remote_server.client_times(
        {"record": {"start": 0, "lines": [[10, transcript.said_to_wire(said)]]}}
    )
    assert parse(lines.lines[0][1].occurred_at) <= latest


def test_a_fast_client_s_turn_keeps_its_order(store, monkeypatch):
    """Clamped one time at a time, a client 90 s fast lost its order: a reply said 10 s into the turn kept its time
    (within the minute of the Stop that sent it), the next prompt was clamped to now, and the reply sorted after it
    and joined the next turn.  Every time a request carries moves back by the same lead."""
    root, homes = store
    remote_server.write_server_config(
        homes["claude-code"], "claude-code", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    config = remote_server.load_server_config(homes["claude-code"], "claude-code")
    start = datetime.now(timezone.utc)

    class Clock(datetime):
        at = start

        @classmethod
        def now(cls, tz=None):
            return cls.at

    monkeypatch.setattr(remote_server, "datetime", Clock)

    def client(seconds):  # the client's clock, 90 s fast
        return (start + timedelta(seconds=90 + seconds)).isoformat().replace("+00:00", "Z")

    def prompt(text, prompt_id, seconds):
        remote_server.handle_request(
            config,
            {
                "payload": {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "TEST-fast-session",
                    "prompt_id": prompt_id,
                    "prompt": text,
                    "cwd": "C:/work",
                },
                "observed_at": client(seconds),
            },
        )

    prompt("TEST 第一问", "TEST-p1", 0)
    Clock.at = start + timedelta(seconds=60)
    lines = [
        transcript.Said("TEST-u1", "user", "TEST 第一问", client(0), "TEST-p1"),
        transcript.Said("TEST-a1", "assistant", "TEST 先答一句", client(10)),
        transcript.Said("TEST-f1", "assistant", "TEST 答完了", client(59)),
    ]
    remote_server.handle_request(
        config,
        {
            "payload": {
                "hook_event_name": "Stop",
                "session_id": "TEST-fast-session",
                "prompt_id": "TEST-p1",
                "cwd": "C:/work",
                "last_assistant_message": "TEST 答完了",
            },
            "record": {
                "start": 0,
                "lines": [[10 * (index + 1), transcript.said_to_wire(said)] for index, said in enumerate(lines)],
            },
            "observed_at": client(60),
        },
    )
    Clock.at = start + timedelta(seconds=70)
    prompt("TEST 第二问", "TEST-p2", 70)
    when = {}
    for _role, _origin, content, at in _rows(root, "workpc-claude-code"):
        when[content] = min(when.get(content, at), at)
    assert when["TEST 第一问"] < when["TEST 先答一句"] < when["TEST 答完了"] < when["TEST 第二问"], when
    assert all(
        datetime.fromisoformat(at.replace("Z", "+00:00")) <= start + timedelta(seconds=70) for at in when.values()
    ), when


def test_a_codex_hook_is_kept_before_it_is_sent(tmp_path, monkeypatch):
    """Codex ends SessionEnd and Interrupt at 3 s.  With the interpreter's start and a connection that does not
    open, a hook that waited for the server was ended before it could keep anything."""
    config = _client(tmp_path, "codex", _free_port())
    folder = config["state_dir"] / "spool"
    seen = []
    monkeypatch.setattr(
        remote_client,
        "_post",
        lambda config, body, timeout: seen.append(len(list(folder.glob("*.json")))) or {"result": {}, "through": None},
    )
    monkeypatch.setattr(remote_client, "_start_flush", lambda config: None)
    _hook(
        config,
        {"hook_event_name": "SessionEnd", "session_id": "TEST-codex-session", "reason": "exit", "cwd": "C:/work"},
    )
    assert seen == [1], "kept while it was being sent"
    assert list(folder.glob("*.json")) == [], "and removed once it was stored"


def test_a_hook_the_busy_store_did_not_store_is_kept_to_send_again(store, tmp_path, monkeypatch):
    """The server answered 200 whatever became of the capture, and the client took that as delivered: a message
    the busy store could not take was lost, where sending it again a minute later would have stored it."""
    from scope_recall.contracts import ContractError
    from scope_recall.core import MemoryCore

    root, homes = store
    remote_server.write_server_config(
        homes["codex"], "codex", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    server = remote_server.load_server_config(homes["codex"], "codex")
    body = {
        "payload": {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-codex-session",
            "turn_id": "TEST-busy",
            "prompt": "TEST 忙的时候说的一句。",
            "cwd": "C:/work",
        }
    }

    def busy(self, *args, **kwargs):
        raise ContractError("DEADLINE_EXCEEDED", "writer_lease")

    with monkeypatch.context() as patched:
        patched.setattr(MemoryCore, "record_host_event", busy)
        assert remote_server.handle_request(server, body)["retry"] is True
    assert remote_server.handle_request(server, body)["retry"] is False

    config = _client(tmp_path, "codex", _free_port())
    recalled = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "TEST 记忆"}}
    monkeypatch.setattr(
        remote_client, "_post", lambda config, body, timeout: {"result": recalled, "through": None, "retry": True}
    )
    monkeypatch.setattr(remote_client, "_start_flush", lambda config: None)
    assert _hook(config, body["payload"]) == recalled, "the recall is used"
    assert len(list((config["state_dir"] / "spool").glob("*.json"))) == 1, "and the hook is kept to send again"


def test_a_locked_database_fails_the_capture_or_the_recall_not_the_request(store, monkeypatch):
    """SQLite's own "database is locked" (``BEGIN IMMEDIATE`` past its wait) escaped the hook: the server answered 500
    and the prompt got no recall at all, as fifteen of the work computer's hooks did in one second on 2026-09-28
    (rc13).  The capture fails and the hook is kept to send again; a recall that meets it fails alone."""
    import sqlite3

    from scope_recall.core import MemoryCore

    root, homes = store
    remote_server.write_server_config(
        homes["codex"], "codex", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    server = remote_server.load_server_config(homes["codex"], "codex")
    body = {
        "payload": {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-codex-session",
            "turn_id": "TEST-locked",
            "prompt": "TEST 库被锁住时说的一句。",
            "cwd": "C:/work",
        }
    }

    def locked(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patched:
        patched.setattr(MemoryCore, "record_host_event", locked)
        answer = remote_server.handle_request(server, body)
    assert answer["retry"] is True, "kept to send again"
    assert all(type(answer[key]) is int for key in ("build_ms", "capture_ms", "attach_ms", "close_ms")), answer
    with monkeypatch.context() as patched:
        patched.setattr(MemoryCore, "recall_packet", locked)
        answer = remote_server.handle_request(server, dict(body, payload=dict(body["payload"], turn_id="TEST-l2")))
    assert (answer["result"], answer["reason"], answer["retry"]) == ({}, "recall_exception", False)
    assert answer["recall_error"] == "OperationalError"

    def broken(self, *args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    # A store broken otherwise than busy is named, where the answer said nothing of it (review of rc13).
    with monkeypatch.context() as patched:
        patched.setattr(MemoryCore, "record_host_event", broken)
        answer = remote_server.handle_request(server, dict(body, payload=dict(body["payload"], turn_id="TEST-l3")))
    assert (answer["retry"], answer["error"]) == (True, "DatabaseError")


def test_a_hook_whose_message_s_key_was_deleted_is_not_sent_again(store, monkeypatch):
    """A copy of a deleted message under that message's key is refused for good and leaves the inbox cancelled (rc13):
    sent again, it met the same refusal on every try."""
    from scope_recall.core import MemoryCore
    from scope_recall.core.capture import CaptureReceipt
    from scope_recall.core.capture_inbox import SOURCE_DELETED_GAP

    root, homes = store
    remote_server.write_server_config(
        homes["codex"], "codex", listen="127.0.0.1", port=_free_port(), token_sha256="0" * 64
    )
    server = remote_server.load_server_config(homes["codex"], "codex")
    body = {
        "payload": {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-codex-session",
            "turn_id": "TEST-deleted",
            "prompt": "TEST 键已删除的一句。",
            "cwd": "C:/work",
        }
    }
    refused = CaptureReceipt(
        "cancelled", (), "not_persisted", "unchanged", "unchanged", (SOURCE_DELETED_GAP,), "ACCESS_DENIED"
    )
    monkeypatch.setattr(MemoryCore, "record_host_event", lambda self, *args, **kwargs: refused)
    assert remote_server.handle_request(server, body)["retry"] is False


def test_a_refused_request_is_read_before_it_is_answered():
    """Answered before its body was read, the connection closed with the request unread, and Windows resets such a
    socket: the client got WinError 10053 instead of the 401 (CI, 2026-09-28)."""
    import asyncio

    events = []
    chunks = [
        {"type": "http.request", "body": b"TEST" * 10, "more_body": True},
        {"type": "http.request", "body": b"TEST", "more_body": False},
    ]

    async def receive():
        events.append("receive")
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        events.append(message["type"])

    gate = remote_server._TokenGate(None, "0" * 64)
    asyncio.run(
        gate(
            {"type": "http", "method": "POST", "path": "/mcp", "headers": [], "client": ("127.0.0.1", 1)}, receive, send
        )
    )
    assert events[:3] == ["receive", "receive", "http.response.start"], events


def test_half_of_a_broken_emoji_reaches_the_entry(served, tmp_path):
    """Sent as strict UTF-8, a prompt holding a lone surrogate (half of a broken emoji, which JavaScript writes)
    failed its request, and the turn had no recall; the server refused such a payload for good (review of rc11)."""
    root, homes, ports = served
    config = _client(tmp_path, "claude-code", ports["claude-code"])
    _hook(
        config,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-work-session",
            "prompt_id": "TEST-p8",
            "prompt": "TEST 表情坏了" + chr(0xD83D),
            "cwd": "C:/work",
        },
    )
    server = remote_server.load_server_config(homes["claude-code"], "claude-code")
    remote_server.handle_request(
        server,
        {
            "payload": {
                "hook_event_name": "Stop",
                "session_id": "TEST-work-session",
                "prompt_id": "TEST-p8",
                "cwd": "C:/work",
                "last_assistant_message": "TEST 回复也坏了" + chr(0xDC00),
            }
        },
    )
    said = [content for _role, _origin, content, _at in _rows(root, "workpc-claude-code")]
    assert "TEST 表情坏了" + chr(0xFFFD) in said and "TEST 回复也坏了" + chr(0xFFFD) in said


def test_a_hook_nested_past_the_parser_s_limit_is_answered_empty(tmp_path, past_the_parser):
    """A payload nested past what the JSON parser takes ended the remote hook with a RecursionError (review of rc11)."""
    config = _client(tmp_path, "claude-code", _free_port())
    raw = past_the_parser(b'{"hook_event_name": "UserPromptSubmit", "prompt": ', b"}")
    assert remote_client.run_hook(config, raw) == {}


def test_the_server_refuses_a_body_nested_past_the_parser_s_limit(served, past_the_parser):
    """A request nested past what the parser takes raised out of the server as a 500, which a Codex client keeps to
    send again for good (review of rc11)."""
    _root, _homes, ports = served
    body = past_the_parser(b'{"payload": ', b"}")
    request = urllib.request.Request(
        f"http://127.0.0.1:{ports['claude-code']}/hook",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}-claude-code"},
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=20)
    assert refused.value.code == 400


def test_a_remote_prompt_s_recall_is_the_server_s_kept_recaller_s(store, tmp_path, monkeypatch):
    """The work computer's prompts were recalled by a handler made for each request, which opened its vector table
    and embedding worker every time: 3-5 s of a 5 s budget, a third of them without their vector search on
    2026-09-29 (rc12).  A prompt's recall is asked of the server's kept recaller; the request's handler stores the
    prompt, and recalls itself only while the kept one is busy.  This store has no runtime config, so a hook gets
    the 2 s default, and a slow CI runner's capture left less than the second a kept recall is asked with: the
    entries' own configs give 6 s."""
    from scope_recall.adapters.clients import handler as handler_module

    monkeypatch.setattr(handler_module, "_TOTAL_BUDGET_S", 10.0)
    _root, homes = store
    config = remote_server.RemoteServerConfig(
        home=homes["claude-code"], host="claude-code", listen="127.0.0.1", port=1, token_sha256="0" * 64
    )
    asked = []

    def kept(payload, current_refs, gaps, budget, **kwargs):
        asked.append(payload["hook_event_name"])
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "TEST warm"}}, {
            "recall_vectors": True,
            "recall_vector_gap": None,
            "recall_error_detail": None,
            "last_reason": None,
        }

    body = {
        "payload": {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-warm-session",
            "prompt_id": "TEST-warm-1",
            "prompt": "TEST warm recall prompt",
            "cwd": "C:/work",
        }
    }
    answer = remote_server.handle_request(config, body, recaller=kept)
    assert asked == ["UserPromptSubmit"] and answer["warm"] == "answered"
    assert answer["result"]["hookSpecificOutput"]["additionalContext"] == "TEST warm"
    with closing(sqlite3.connect(_root / "memory.sqlite3")) as connection:
        stored = [
            row[0]
            for row in connection.execute(
                "SELECT content FROM source_events UNION ALL SELECT payload_json FROM capture_inbox"
            )
        ]
    assert any("TEST warm recall prompt" in (text or "") for text in stored), "the request's own handler stored it"
    busy = remote_server.handle_request(
        config, {"payload": {**body["payload"], "prompt_id": "TEST-warm-2"}}, recaller=lambda *args, **kwargs: None
    )
    assert busy["warm"] == "busy" and busy["result"] != answer["result"]
    stop = remote_server.handle_request(
        config,
        {"payload": {"hook_event_name": "Stop", "session_id": "TEST-warm-session", "cwd": "C:/work"}},
        recaller=kept,
    )
    assert asked == ["UserPromptSubmit"] and stop["warm"] is None, "only a prompt's recall is asked"


def test_the_server_log_says_how_the_kept_recall_went(served, tmp_path, monkeypatch):
    _root, homes, ports = served
    monkeypatch.setattr(
        remote_server,
        "handle_request",
        lambda config, body, started=None, recaller=None: {
            "result": {},
            "through": None,
            "reason": None,
            "error": None,
            "recall_error": None,
            "recall_vector": None,
            "warm": "answered" if recaller is not None else "no recaller",
            "build_ms": 120,
            "capture_ms": 850,
            "attach_ms": None,
            "close_ms": 40,
        },
    )
    root_logger = logging.getLogger()
    level = root_logger.level
    handler = remote_server.log_to_file(homes["codex"])
    try:
        _hook(
            _client(tmp_path, "codex", ports["codex"]),
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-t9",
                "prompt": "TEST 热的。",
                "cwd": "C:/work",
            },
        )
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(level)
        handler.close()
    log = (homes["codex"] / "scope-recall" / remote_server.LOG_NAME).read_text(encoding="utf-8")
    assert "hook UserPromptSubmit: None, record through None, " in log and ", warm recall answered" in log
    # Where the time went (rc13); a runtime the handler did not attach is left out.
    assert " ms (build 120 ms, capture 850 ms, close 40 ms), warm recall" in log


def test_a_kept_recall_that_raised_is_named_and_the_request_recalls_itself(store, monkeypatch, caplog):
    """A kept recall that raised was swallowed by the request's handler, and the server's log said nothing (review of
    rc12)."""
    import logging

    from scope_recall.adapters.clients import handler as handler_module

    monkeypatch.setattr(handler_module, "_TOTAL_BUDGET_S", 10.0)
    _root, homes = store
    config = remote_server.RemoteServerConfig(
        home=homes["claude-code"], host="claude-code", listen="127.0.0.1", port=1, token_sha256="0" * 64
    )

    def kept(*args, **kwargs):
        raise RuntimeError("TEST kept recall raised")

    body = {
        "payload": {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "TEST-warm-session",
            "prompt_id": "TEST-warm-raised",
            "prompt": "TEST raised prompt",
            "cwd": "C:/work",
        }
    }
    with caplog.at_level(logging.WARNING, logger=remote_server._log.name):
        answer = remote_server.handle_request(config, body, recaller=kept)
    assert answer["warm"] == "failed:RuntimeError"
    assert any("kept recall failed" in record.getMessage() and record.exc_info for record in caplog.records)


def test_a_workbuddy_client_s_turn_and_record_reach_its_entry_once(store, tmp_path, monkeypatch):
    """A WorkBuddy on another machine is forwarded as the claude-code host is: its Stop sends what its own session
    record shows (read with WorkBuddy's reader, the record found by its session when the hook names it wrongly), and
    the server stores each message once.  A Stop that only repeats the last reply sends nothing new."""
    root, _homes = store
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    home = tmp_path / "TEST-workpc-workbuddy-home"
    attach_shared_record(
        root,
        client_entry_record(
            host="workbuddy",
            home=home,
            entry_id="workpc-workbuddy",
            display_name="TEST WorkBuddy",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    server = remote_server.RemoteServerConfig(
        home=home, host="workbuddy", listen="127.0.0.1", port=1, token_sha256="0" * 64
    )
    client = _client(tmp_path, "workbuddy", _free_port())
    sent = []

    def post(config, body, timeout):
        sent.append(body)
        return remote_server.handle_request(server, json.loads(json.dumps(body)))

    monkeypatch.setattr(remote_client, "_post", post)
    projects = tmp_path / "TEST-workpc-projects"
    monkeypatch.setattr(transcript, "workbuddy_projects", lambda: projects)
    start = int((datetime.now(timezone.utc) - timedelta(seconds=30)).timestamp() * 1000)
    record = projects / "c--work" / "TEST-wb-session.jsonl"
    record.parent.mkdir(parents=True)
    lines = [
        {
            "type": "message",
            "role": "user",
            "id": "u1",
            "timestamp": start,
            "content": [{"type": "input_text", "text": "<user_query>TEST 看一下\nQX-17。</user_query>"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "a1",
            "timestamp": start + 1000,
            "content": [{"type": "output_text", "text": "TEST 我先查记录。"}],
        },
        {"type": "function_call", "id": "f1", "timestamp": start + 2000, "name": "TEST-ls", "arguments": "{}"},
        {
            "type": "message",
            "role": "assistant",
            "id": "a2",
            "timestamp": start + 3000,
            "content": [{"type": "output_text", "text": "TEST QX-17 已完成。"}],
        },
    ]
    record.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    base = {"session_id": "TEST-wb-session", "cwd": "C:/work"}
    _hook(client, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "TEST 看一下QX-17。"})
    stop = {
        **base,
        "hook_event_name": "Stop",
        "transcript_path": str(record)[:-1],
        "last_assistant_message": "TEST QX-17 已完成。",
    }
    _hook(client, stop)
    _hook(client, stop)
    assert "record" not in sent[0] and sent[1]["record"]["start"] == 0 and "record" not in sent[2]
    said = sorted((role, content) for role, _origin, content, _at in _rows(root, "workpc-workbuddy"))
    assert said == sorted(
        [("user", "TEST 看一下QX-17。"), ("assistant", "TEST 我先查记录。"), ("assistant", "TEST QX-17 已完成。")]
    )
    cursor = transcript.Cursor(client["state_dir"], "TEST-wb-session", record)
    assert cursor.load() == record.stat().st_size, "the cursor moves as far as the server stored"


WB_NOTICE = "TEST Authentication required. Please use /login command to sign in to your account"


def _remote_workbuddy(store, tmp_path, monkeypatch, post):
    """A WorkBuddy on another machine, its entry beside the store's others, its posts handled by ``post``; its record."""
    root, _homes = store
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    home = tmp_path / "TEST-workpc-workbuddy-home"
    attach_shared_record(
        root,
        client_entry_record(
            host="workbuddy",
            home=home,
            entry_id="workpc-workbuddy",
            display_name="TEST WorkBuddy",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    server = remote_server.RemoteServerConfig(
        home=home, host="workbuddy", listen="127.0.0.1", port=1, token_sha256="0" * 64
    )
    client = _client(tmp_path, "workbuddy", _free_port())
    monkeypatch.setattr(remote_client, "_post", lambda config, body, timeout: post(server, body))
    projects = tmp_path / "TEST-workpc-projects"
    monkeypatch.setattr(transcript, "workbuddy_projects", lambda: projects)
    record = projects / "c--work" / "TEST-wb-session.jsonl"
    record.parent.mkdir(parents=True)
    start = int((datetime.now(timezone.utc) - timedelta(seconds=30)).timestamp() * 1000)
    lines = [
        {
            "type": "message",
            "role": "user",
            "id": "u1",
            "timestamp": start,
            "content": [{"type": "input_text", "text": "<user_query>TEST 问一下。</user_query>"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "a1",
            "timestamp": start + 1000,
            "status": "incomplete",
            "content": [{"type": "output_text", "text": WB_NOTICE}],
            "providerData": {"error": {"message": WB_NOTICE}},
        },
    ]
    record.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    return root, client, record


def test_a_remote_workbuddy_s_error_shown_in_place_of_a_reply_is_not_stored(store, tmp_path, monkeypatch):
    """The client on the other machine has the record: it judges the Stop's reply there and tells the server, which
    stores the person's prompt and not the error; the lines it sends skip the error as well."""
    root, client, record = _remote_workbuddy(
        store,
        tmp_path,
        monkeypatch,
        lambda server, body: remote_server.handle_request(server, json.loads(json.dumps(body))),
    )
    base = {"session_id": "TEST-wb-session", "cwd": "C:/work"}
    _hook(client, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "TEST 问一下。"})
    _hook(
        client, {**base, "hook_event_name": "Stop", "transcript_path": str(record), "last_assistant_message": WB_NOTICE}
    )
    said = sorted((role, content) for role, _origin, content, _at in _rows(root, "workpc-workbuddy"))
    assert said == [("user", "TEST 问一下。")]


def test_a_server_does_not_open_a_workbuddy_record_to_judge_a_remote_reply(store, tmp_path, monkeypatch):
    """Whether a Stop's reply is an error WorkBuddy showed is read from the session record.  The server of a client on
    another machine never looks for one (a request names no path it may open, and a record of that session in its own
    WorkBuddy folders is not the client's): it takes the client's word."""
    judged = []
    real = transcript.workbuddy_error_reply

    def server_side(server, body):
        monkeypatch.setattr(transcript, "workbuddy_error_reply", lambda path, reply: judged.append(path) or True)
        try:
            return remote_server.handle_request(server, json.loads(json.dumps(body)))
        finally:
            monkeypatch.setattr(transcript, "workbuddy_error_reply", real)

    root, client, record = _remote_workbuddy(store, tmp_path, monkeypatch, server_side)
    _hook(
        client,
        {
            "session_id": "TEST-wb-session",
            "cwd": "C:/work",
            "hook_event_name": "Stop",
            "transcript_path": str(record),
            "last_assistant_message": WB_NOTICE,
        },
    )
    assert judged == [], "the server opened the record a request named"
    assert [row for row in _rows(root, "workpc-workbuddy") if row[0] == "assistant"] == []


def test_a_workbuddy_subagent_s_stop_sends_no_record(tmp_path, monkeypatch):
    """A subagent's Stop (its record id ``agent-*``, its record in a ``subagents`` folder) ends no turn of the person's
    session: its record is neither read nor sent, and no cursor moves for it."""
    client = _client(tmp_path, "workbuddy", _free_port())
    sent = []
    monkeypatch.setattr(remote_client, "_post", lambda config, body, timeout: sent.append(body) or {})
    record = tmp_path / "TEST-projects" / "c--work" / "TEST-wb-session" / "subagents" / "agent-TEST1.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps(
            {
                "type": "message",
                "role": "assistant",
                "id": "a1",
                "timestamp": 1759320000123,
                "content": [{"type": "output_text", "text": "TEST 子代理的话。"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _hook(
        client,
        {
            "session_id": "TEST-wb-session",
            "cwd": "C:/work",
            "hook_event_name": "Stop",
            "agent_id": "agent-TEST1",
            "transcript_path": str(record),
            "last_assistant_message": "TEST 子代理的话。",
        },
    )
    assert len(sent) == 1 and "record" not in sent[0]
    assert transcript.Cursor(client["state_dir"], "TEST-wb-session", record).load() == 0
