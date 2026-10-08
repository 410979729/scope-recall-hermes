"""A local client attached to a shared store: Claude Code, through the Codex adapter.

Two Hermes homes and a Claude Code home share one store.  What the owner types
into Claude Code is the owner's, recorded under the client's entry, and a Hermes
entry recalls it; what a Hermes entry was told reaches the client's prompt.
Sources are synthetic; nothing here is a person's memory.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.clients.config import CodexConfigError, load_shared_client
from scope_recall.adapters.clients.mcp_server import build_server
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter
from scope_recall.adapters.hermes.authorization import build_ingress_authorizer
from scope_recall.adapters.hermes.identity import host_scope_payload, principal_ref
from scope_recall.adapters.hermes.installation import (
    attach_shared_entry,
    attach_shared_record,
    build_installation_manifest,
    client_entry_record,
    load_binding_for_home,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)
from scope_recall.contracts import ContractError, InstanceBinding
from scope_recall.core.capture import CaptureReceipt

NOW = "2026-09-24T20:00:00Z"
AGENT = "TEST-agent"
WORKSPACE = "TEST-workspace"
OWNER = "TEST-owner"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "TEST-shared"
    write_shared_payload(root, new_shared_payload(root, agent_id=AGENT))
    homes = {}
    for name, display in (("tianshu", "天枢"), ("tianquan", "天权")):
        home = tmp_path / f"TEST-{name}-home"
        home.mkdir()
        attach_shared_entry(
            root,
            build_installation_manifest(home, agent_id=AGENT, user_id=OWNER, agent_workspace=WORKSPACE),
            entry_id=name,
            display_name=display,
            now=NOW,
        )
        homes[name] = home
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    client = tmp_path / "TEST-claude-code-home"
    attach_shared_record(
        root,
        client_entry_record(
            host="claude-code",
            home=client,
            entry_id="claude-code",
            display_name="Claude Code",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    return root, homes, client, owner["capture_scope_id"]


def _prompt(text, *, session="TEST-cc-session", prompt_id="TEST-prompt-1", cwd="C:/anywhere/at/all"):
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "prompt_id": prompt_id,
        "prompt": text,
        "cwd": cwd,
        "transcript_path": "C:/TEST/transcript.jsonl",
        "permission_mode": "default",
    }


def _hook(client):
    # The system clock, as the Hermes entries use: a fixed earlier "now" would hide their sources as future ones.
    return CodexHookHandler.from_home(str(client), "claude-code")


def _rows(root, sql):
    with closing(sqlite3.connect(root / "memory.sqlite3")) as connection:
        return connection.execute(sql).fetchall()


def _hermes(home):
    provider = ScopeRecallHermesAdapter()
    provider.initialize(
        "TEST-session-1",
        hermes_home=str(home),
        platform="cli",
        agent_context="primary",
        agent_identity=AGENT,
        agent_workspace=WORKSPACE,
        user_id=OWNER,
        parent_session_id="",
    )
    return provider


def test_a_prompt_is_the_owner_s_under_the_client_s_entry_and_a_hermes_entry_recalls_it(store):
    root, homes, client, capture = store
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 青鸟计划的代号是 QX-17。"))
        assert hook.diagnostics.capture_stage == "source_committed"
        hook.handle_payload(
            {
                "hook_event_name": "Stop",
                "session_id": "TEST-cc-session",
                "prompt_id": "TEST-prompt-1",
                "last_assistant_message": "好的，记下了。",
                "cwd": "C:/elsewhere",
            }
        )
    finally:
        hook.close()

    rows = _rows(
        root,
        "SELECT entry_id, session_id, scope_id, role, origin, source_event_key, extra_json "
        "FROM source_events WHERE entry_id='claude-code' ORDER BY role DESC",
    )
    assert [(row[0], row[1], row[2], row[3], row[4]) for row in rows] == [
        ("claude-code", "claude-code:TEST-cc-session", capture, "user", "human_direct"),
        ("claude-code", "claude-code:TEST-cc-session", capture, "assistant", "assistant_visible"),
    ]
    store_id = read_shared_payload(root)["installation_id"]
    assert rows[0][5] == f"claude-code:{store_id}:TEST-cc-session:user:TEST-prompt-1@1"
    # Attaching the client made its local user the owner, the way the Hermes CLI's is.
    assert principal_ref("human", store_id, "claude-code", "local") in rows[0][6]

    asked = _hermes(homes["tianshu"])
    try:
        injected = asked.prefetch("青鸟计划的代号 QX-17 是什么")
    finally:
        asked.shutdown()
    items = json.loads(injected.partition("\n")[2])["items"]
    assert any(
        "QX-17" in item["content"] and item["entries"] == [{"id": "claude-code", "name": "Claude Code"}]
        for item in items
    )


def test_what_a_hermes_entry_was_told_reaches_the_client_s_prompt_marked_as_theirs(store):
    root, homes, client, _capture = store
    told = _hermes(homes["tianquan"])
    try:
        told.on_turn_start(1, "TEST 白鹭项目的负责人是 KZ-42。", turn_id="TEST-turn-1", session_id="TEST-session-1")
        told.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。"
        )
        told.sync_turn("TEST 白鹭项目的负责人是 KZ-42。", "好的。", session_id="TEST-session-1")
    finally:
        told.shutdown()

    hook = _hook(client)
    try:
        result = hook.handle_payload(_prompt("白鹭项目的负责人 KZ-42 是谁", prompt_id="TEST-prompt-2"))
    finally:
        hook.close()
    context = result["hookSpecificOutput"]["additionalContext"]
    guidance, _newline, body = context.partition("\n")
    assert "You are Claude Code (claude-code)" in guidance
    marked = [item for item in json.loads(body)["items"] if "KZ-42" in item["content"]]
    assert marked and all(item["entries"] == [{"id": "tianquan", "name": "天权"}] for item in marked)


def test_the_client_s_tool_traffic_is_not_recorded_and_a_turn_needs_its_prompt_id(store):
    root, _homes, client, _capture = store
    hook = _hook(client)
    try:
        hook.handle_payload(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "TEST-cc-session",
                "prompt_id": "P",
                "tool_name": "Bash",
                "tool_use_id": "T1",
                "tool_input": {"command": "ls"},
                "tool_response": "TEST output",
                "cwd": "C:/x",
            }
        )
        assert hook.diagnostics.last_reason == "unsupported_event"
        prompt = _prompt("TEST no id")
        del prompt["prompt_id"]
        assert hook.handle_payload(prompt) == {}
        assert hook.diagnostics.last_reason == "missing_turn_id"
    finally:
        hook.close()
    assert _rows(root, "SELECT count(*) FROM source_events WHERE entry_id='claude-code'") == [(0,)]


def test_a_session_start_on_an_entry_reads_nothing_of_the_store(store, monkeypatch):
    """A local installation checks its store's status at a session start; on the pilot's shared store that
    count took 7-8 s, past Codex's 2 s hook timeout.  An entry was checked when its config loaded."""
    _root, _homes, client, _capture = store
    hook = _hook(client)
    monkeypatch.setattr(hook.core, "status", lambda *args, **kwargs: pytest.fail("status read at a session start"))
    try:
        assert (
            hook.handle_payload(
                {"hook_event_name": "SessionStart", "session_id": "TEST-cc-session", "cwd": "C:/anywhere"}
            )
            == {}
        )
    finally:
        hook.close()


def _codex_client(root, tmp_path):
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    home = tmp_path / "TEST-codex-home"
    attach_shared_record(
        root,
        client_entry_record(
            host="codex",
            home=home,
            entry_id="codex",
            display_name="Codex",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    return home


def test_a_client_s_prompt_runs_the_entry_s_budget(store, tmp_path):
    """Recall on the pilot's shared store took 2.7-5.7 s.  Claude Code waits 15 s for a prompt's hook and Codex
    now as long, so both run the entry's configured budget; with 2 s most of Codex's automatic recalls came back
    empty.  No installer writes ``hook_processing_seconds``: a config without it runs the worker's 6 s."""
    root, _homes, client, _capture = store
    codex = _codex_client(root, tmp_path)
    for home, host in ((client, "claude-code"), (codex, "codex")):
        (home / "scope-recall" / "runtime-config.json").write_text(
            json.dumps({"hook_processing_seconds": 5.5}), encoding="utf-8"
        )
        hook = CodexHookHandler.from_home(str(home), host)
        try:
            assert hook._hook_budget() == 5.5, host
        finally:
            hook.close()
    (client / "scope-recall" / "runtime-config.json").write_text(
        json.dumps({"auto_recall_seconds": 5.0}), encoding="utf-8"
    )
    hook = _hook(client)
    try:
        assert hook._hook_budget() == 6.0, "a config that does not name the budget runs the worker's default"
    finally:
        hook.close()
    (client / "scope-recall" / "runtime-config.json").write_text(
        json.dumps({"hook_processing_seconds": 60}), encoding="utf-8"
    )
    hook = _hook(client)
    try:
        assert hook._hook_budget() == 2.0, "an out-of-bounds budget falls back to the hook's 2 s"
    finally:
        hook.close()


def test_a_prompt_answers_when_its_work_is_done_not_at_its_ceiling(store, tmp_path):
    """The budget bounds a prompt's capture and recall; it is not a wait.  A small store answers in well under
    the 6 s a hook may take, and the client's prompt goes on as soon as it does."""
    root, _homes, _client, _capture = store
    codex = _codex_client(root, tmp_path)
    # What attach writes: the routes, and no hook budget of its own.
    (codex / "scope-recall" / "runtime-config.json").write_text(
        json.dumps({"auto_recall_seconds": 5.0}), encoding="utf-8"
    )
    hook = CodexHookHandler.from_home(str(codex), "codex")
    try:
        assert hook._hook_budget() == 6.0
        started = datetime.now(timezone.utc)
        hook.handle_payload(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-turn-1",
                "prompt": "TEST 周五之前把 QX-17 的报价发出去。",
                "cwd": "C:/anywhere",
            }
        )
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    finally:
        hook.close()
    assert hook.diagnostics.last_reason is None, hook.diagnostics.last_reason
    assert elapsed < 4.0, f"the hook took {elapsed:.1f} s of its 6 s on a store of a few sources"


def test_a_queued_capture_replays_under_the_client_entry_s_grants_only(store):
    root, _homes, client, capture = store
    config = load_shared_client(client, "claude-code")
    worker = read_shared_payload(root)
    # The shared worker replays every entry's inbox; it binds every scope of the store.
    authorize = build_ingress_authorizer(
        InstanceBinding(
            worker["agent_id"],
            worker["installation_id"],
            root.resolve(),
            frozenset(worker["scope_ids"]),
            worker["test_mode"],
            "shared",
        )
    )
    assert capture in authorize(host_scope_payload(config.scope))
    assert authorize(host_scope_payload(config.scope)) == config.audience.writable_scope_ids
    forged = dict(host_scope_payload(config.scope), platform="telegram")
    assert authorize(forged) == frozenset(), "a route the entry was not granted writes nothing"


def test_a_pointer_binds_only_its_own_host_and_home(store, tmp_path):
    root, homes, client, _capture = store
    with pytest.raises(CodexConfigError):
        load_shared_client(client, "codex")
    with pytest.raises(CodexConfigError):
        load_shared_client(homes["tianshu"], "claude-code")
    copied = tmp_path / "TEST-copied-home"
    (copied / "scope-recall").mkdir(parents=True)
    (copied / "scope-recall" / "attachment.json").write_bytes(
        (client / "scope-recall" / "attachment.json").read_bytes()
    )
    with pytest.raises(CodexConfigError):
        load_shared_client(copied, "claude-code")
    with pytest.raises(Exception, match="another host"):
        load_binding_for_home(client)


def test_the_client_s_tools_read_the_store_and_refuse_to_change_it(store):
    root, _homes, client, _capture = store
    server = build_server(load_shared_client(client, "claude-code"), workspace=None)

    class Request:
        meta = {"threadId": "3f0f5b5e-0000-4000-8000-000000000000"}

    class Ctx:
        request_context = Request()

    status = server.status(Ctx())
    assert status["result"]["entry"] == {"id": "claude-code", "name": "Claude Code"}
    assert "mcp_session_is_not_claude_code_conversation_id" in status["capability_gaps"]
    with pytest.raises(ContractError):
        server.propose_memory(Ctx(), "1.1", "TEST a proposal")
    assert _rows(root, "SELECT count(*) FROM source_events WHERE entry_id='claude-code'") == [(0,)]


# -- the session record ------------------------------------------------------
# Claude Code's hooks carry a turn's prompt and last message; what the model says while it works, and
# anything a hook could not write, is read from the session record at the end of the turn.


def _moments():
    start = datetime.now(timezone.utc) - timedelta(seconds=30)
    return lambda seconds: (start + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _line(kind, uuid, stamp, **fields):
    return {"type": kind, "uuid": uuid, "timestamp": stamp, "sessionId": "TEST-cc-session", **fields}


def _person(uuid, stamp, text, *, prompt_id="TEST-prompt-1"):
    return _line(
        "user", uuid, stamp, origin={"kind": "human"}, promptId=prompt_id, message={"role": "user", "content": text}
    )


def _model(uuid, stamp, *blocks):
    return _line(
        "assistant", uuid, stamp, message={"role": "assistant", "model": "TEST-model", "content": list(blocks)}
    )


def _said(text):
    return {"type": "text", "text": text}


def _record(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _stop(record, last=None):
    payload = {
        "hook_event_name": "Stop",
        "session_id": "TEST-cc-session",
        "prompt_id": "TEST-prompt-1",
        "transcript_path": str(record),
        "cwd": "C:/x",
        "stop_hook_active": False,
    }
    if last is not None:
        payload["last_assistant_message"] = last
    return payload


def _said_in_store(root):
    return sorted(_rows(root, "SELECT role, origin, content FROM source_events WHERE entry_id='claude-code'"))


def test_a_stop_records_what_the_session_record_shows_was_said_and_nothing_else(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("u1", at(0), "TEST 帮我查一下 QX-17 的进度。"),
        _model("a1", at(1), {"type": "thinking", "thinking": "TEST unseen"}),
        _model("a2", at(2), _said("TEST 我先看一下记录。")),
        _model("a3", at(3), {"type": "tool_use", "id": "T1", "name": "Bash", "input": {"command": "ls"}}),
        _line(
            "user",
            "t1",
            at(4),
            message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "T1", "content": "TEST tool output"}],
            },
        ),
        _line(
            "attachment",
            "q1",
            at(5),
            attachment={
                "type": "queued_command",
                "commandMode": "prompt",
                "origin": {"kind": "human"},
                "prompt": "TEST 顺便看看 KZ-42。",
            },
        ),
        _line(
            "attachment",
            "n1",
            at(6),
            attachment={
                "type": "queued_command",
                "commandMode": "task-notification",
                "prompt": "<task-notification>TEST</task-notification>",
            },
        ),
        _line(
            "user",
            "n2",
            at(7),
            origin={"kind": "task-notification"},
            message={"role": "user", "content": "<task-notification>TEST</task-notification>"},
        ),
        _line(
            "user",
            "s1",
            at(8),
            isCompactSummary=True,
            message={"role": "user", "content": "TEST summary of earlier work"},
        ),
        _line("user", "m1", at(9), isMeta=True, message={"role": "user", "content": "TEST meta"}),
        _model("a4", at(10), _said("TEST QX-17 已经完成。")),
    )
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 帮我查一下 QX-17 的进度。"))
        hook.handle_payload(_stop(record, last="TEST QX-17 已经完成。"))
    finally:
        hook.close()
    # The prompt and the last message came through their hooks as well; each is stored once.
    assert _said_in_store(root) == sorted(
        [
            ("user", "human_direct", "TEST 帮我查一下 QX-17 的进度。"),
            ("assistant", "assistant_visible", "TEST 我先看一下记录。"),
            ("user", "human_direct", "TEST 顺便看看 KZ-42。"),
            ("assistant", "assistant_visible", "TEST QX-17 已经完成。"),
        ]
    )


def test_a_long_prompt_is_stored_once_when_the_record_shows_it_again(store, tmp_path):
    """A prompt over 65,536 characters is stored in segments under keys of their own, so the Stop's read of the
    session record did not find it by its prompt id and stored it a second time (review of 3.4.0rc10)."""
    root, _homes, client, _capture = store
    text = "TEST 很长的提问。" + "长" * 70000
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl", _person("u1", _moments()(0), text))
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt(text))
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    groups = _rows(
        root,
        "SELECT source_group_key,count(*) FROM source_events WHERE role='user' AND entry_id='claude-code' GROUP BY 1",
    )
    assert len(groups) == 1 and groups[0][1] == 2, groups


def test_later_identical_human_message_with_a_different_prompt_id_is_preserved(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl", _person("u1", at(0), "TEST 好"))
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 好"))
        hook.handle_payload(_stop(record))
        _record(record, _person("u2", at(1), "TEST 好", prompt_id="TEST-prompt-2"))
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    store_id = read_shared_payload(root)["installation_id"]
    assert sorted(
        _rows(root, "SELECT source_event_key FROM source_events WHERE role='user' AND entry_id='claude-code'")
    ) == sorted(
        [
            (f"claude-code:{store_id}:TEST-cc-session:user:TEST-prompt-1@1",),
            (f"claude-code:{store_id}:TEST-cc-session:record:u2@1",),
        ]
    )


def test_malformed_record_character_does_not_hold_back_later_messages(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    record = tmp_path / "TEST-projects" / "TEST-cc-session.jsonl"
    record.parent.mkdir(parents=True)
    with record.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(_person("bad", at(0), "TEST \ud800")) + "\n")
        handle.write(json.dumps(_person("good", at(1), "TEST normal")) + "\n")
    hook = _hook(client)
    try:
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    # Half of a broken emoji is kept as U+FFFD with the rest of its message, which had been skipped and lost.
    assert sorted(content for _role, _origin, content in _said_in_store(root)) == ["TEST normal", "TEST " + chr(0xFFFD)]


def test_record_check_carries_stop_budget_and_defers_large_schema_upgrade(store, tmp_path, monkeypatch):
    root, _homes, client, capture_scope = store
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl", _person("u1", _moments()(0), "TEST budgeted record")
    )
    hook = _hook(client)
    original = hook.core.said_in_session
    budgets = []

    def observed(context, scope_id, items, **kwargs):
        budgets.append(kwargs["remaining_seconds"])
        return original(context, scope_id, items, **kwargs)

    monkeypatch.setattr(hook.core, "said_in_session", observed)
    try:
        hook.handle_payload(_stop(record))
        assert budgets and 0 < budgets[0] < 60
        assert ("user", "human_direct", "TEST budgeted record") in _said_in_store(root)

        # Simulate a previously installed large truth store awaiting an upgrade.
        # A Stop pre-check must refuse that upgrade rather than run it in the hook.
        with sqlite3.connect(root / "memory.sqlite3") as db:
            db.execute("PRAGMA user_version=1108")
        monkeypatch.setattr("scope_recall.core.storage._store_bytes", lambda _conn: 200_000_000)
        from scope_recall.contracts import TrustedContext

        context = TrustedContext(
            hook.core.config.binding, "claude-code:TEST-cc-session", hook.config.scope_ids, "host_generated"
        )
        with pytest.raises(ContractError, match="upgrade_pending"):
            original(
                context, capture_scope, [("user", "TEST different", _moments()(1), None)], remaining_seconds=budgets[0]
            )
        with sqlite3.connect(root / "memory.sqlite3") as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1108
    finally:
        hook.close()


def test_a_locked_database_during_the_record_check_ends_the_read_not_the_hook(store, tmp_path, monkeypatch):
    """SQLite's own "database is locked" escaped the Stop hook's check of the session record, which ended the hook
    (rc13).  The read ends there instead, and the next Stop starts again from the same line."""
    root, _homes, client, _capture = store
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl", _person("u1", _moments()(0), "TEST locked record")
    )
    hook = _hook(client)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    try:
        with monkeypatch.context() as patched:
            patched.setattr(hook.core, "said_in_session", locked)
            assert hook.handle_payload(_stop(record)) == {}
        assert hook.diagnostics.capture_error_type == "OperationalError", "named for the server's log (rc13)"
        assert ("user", "human_direct", "TEST locked record") not in _said_in_store(root)
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert ("user", "human_direct", "TEST locked record") in _said_in_store(root)


def test_a_deleted_message_in_the_session_record_does_not_stop_its_read(store, tmp_path, monkeypatch):
    """A record line whose message was deleted is refused for good (``ACCESS_DENIED:source_unavailable``), but the read
    took the refusal as a store it might write later, and every later Stop stopped at that line (review of rc13).  It
    counts as settled, and the read goes on."""
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("gone", at(0), "TEST 被删的记录行", prompt_id="TEST-prompt-gone"),
        _person("next", at(1), "TEST 后面的一行", prompt_id="TEST-prompt-next"),
    )
    hook = _hook(client)
    original = hook.core.record_event

    def deleted(context, event, **kwargs):
        if event["content"] == "TEST 被删的记录行":
            raise ContractError("ACCESS_DENIED", "source_unavailable")
        return original(context, event, **kwargs)

    monkeypatch.setattr(hook.core, "record_event", deleted)
    try:
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    said = [content for _role, _origin, content in _said_in_store(root)]
    assert "TEST 后面的一行" in said and "TEST 被删的记录行" not in said


def test_a_stop_s_capture_time_is_all_of_its_captures(store, tmp_path, monkeypatch):
    """The remote server logs a hook's capture time; for a Stop it was only the last record line's, each capture
    writing over the one before (review of rc13).  All of a hook's captures count."""
    import time

    _root, _homes, client, _capture = store
    at = _moments()
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("one", at(0), "TEST 第一行", prompt_id="TEST-prompt-one"),
        _person("two", at(1), "TEST 第二行", prompt_id="TEST-prompt-two"),
    )
    hook = _hook(client)
    original = hook.core.record_event

    def slow(context, event, **kwargs):
        time.sleep(0.06)
        return original(context, event, **kwargs)

    monkeypatch.setattr(hook.core, "record_event", slow)
    try:
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    total, last = hook.diagnostics.capture_total_ms, hook.diagnostics.capture_elapsed_ms
    assert total >= 120 and total > last, (total, last)


def test_two_record_messages_repeating_hook_text_are_not_both_suppressed(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    text = "TEST 好。"
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("u1", at(0), text),
        _person("u2", at(1), text, prompt_id="TEST-prompt-2"),
    )
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt(text))
        hook.handle_payload(_stop(record))
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert _said_in_store(root).count(("user", "human_direct", text)) == 2


def test_a_queued_message_its_hook_stored_is_not_stored_again(store, tmp_path):
    """The record's queued command carries no promptId; it is known by its words and moment instead."""
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("u1", at(0), "TEST 帮我查一下 QX-17 的进度。"),
        _line(
            "attachment",
            "q1",
            at(1),
            attachment={
                "type": "queued_command",
                "commandMode": "prompt",
                "origin": {"kind": "human"},
                "prompt": "TEST 顺便看看 KZ-42。",
            },
        ),
    )
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 帮我查一下 QX-17 的进度。"))
        hook.handle_payload(_prompt("TEST 顺便看看 KZ-42。", prompt_id="TEST-prompt-queued"))
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert [content for role, _origin, content in _said_in_store(root) if role == "user"] == sorted(
        ["TEST 帮我查一下 QX-17 的进度。", "TEST 顺便看看 KZ-42。"]
    )


def test_a_prompt_still_in_the_inbox_is_not_stored_again_from_the_record(store, tmp_path, monkeypatch):
    root, _homes, client, _capture = store
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("u1", _moments()(0), "TEST 帮我查一下 QX-17 的进度。"),
    )
    from scope_recall.core import capture_inbox

    written = capture_inbox.record_event

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("TEST database is locked")

    hook = _hook(client)
    try:
        monkeypatch.setattr(capture_inbox, "record_event", busy)
        hook.handle_payload(_prompt("TEST 帮我查一下 QX-17 的进度。"))
        assert hook.diagnostics.capture_stage == "durable_inbox"
        monkeypatch.setattr(capture_inbox, "record_event", written)
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    stored = [content for role, _origin, content in _said_in_store(root) if role == "user"]
    waiting = _rows(root, "SELECT count(*) FROM capture_inbox")[0][0]
    assert len(stored) + waiting == 1, "the prompt is stored, or still waiting in the inbox, once"
    assert _rows(root, "SELECT count(*) FROM source_events WHERE source_event_key LIKE '%:record:u1@1'") == [(0,)]


def test_what_could_not_be_written_is_recorded_at_the_next_stop_once(store, tmp_path, monkeypatch):
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("u1", at(0), "TEST 第一句。"),
        _model("a1", at(1), _said("TEST 第一段。")),
        _model("a2", at(2), _said("TEST 第二段。")),
    )
    hook = _hook(client)
    written = hook.core.record_event
    calls = []

    def busy_the_second_time(*args, **kwargs):
        calls.append(None)
        if len(calls) == 2:
            # What the core answers when another process holds the writer lease past the wait.
            return CaptureReceipt("unavailable", (), "unknown", "unknown", "unknown", error_code="STORAGE_UNAVAILABLE")
        return written(*args, **kwargs)

    monkeypatch.setattr(hook.core, "record_event", busy_the_second_time)
    try:
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert [content for _role, _origin, content in _said_in_store(root)] == ["TEST 第一句。"], (
        "the read stops at the message that could not be written"
    )

    _record(record, _model("a3", at(3), _said("TEST 第三段。")))
    hook = _hook(client)
    try:
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert sorted(content for _role, _origin, content in _said_in_store(root)) == sorted(
        ["TEST 第一句。", "TEST 第一段。", "TEST 第二段。", "TEST 第三段。"]
    )


def test_a_lost_or_stale_read_position_costs_a_reread_and_never_a_duplicate(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(
        tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
        _person("u1", at(0), "TEST 一。"),
        _model("a1", at(1), _said("TEST 二。")),
    )
    for _ in range(2):
        hook = _hook(client)
        try:
            hook.handle_payload(_stop(record))
        finally:
            hook.close()
        for kept in (client / "scope-recall" / "transcripts").glob("*.json"):
            kept.unlink()
    # The same name, another record: its opening lines differ, so it is read from the top.
    record.write_text("", encoding="utf-8")
    _record(record, _person("u9", at(5), "TEST 三。"), _person("u1", at(0), "TEST 一。"))
    hook = _hook(client)
    try:
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert sorted(content for _role, _origin, content in _said_in_store(root)) == sorted(
        ["TEST 一。", "TEST 二。", "TEST 三。"]
    )


def test_only_the_session_s_own_record_is_read(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    other = _record(tmp_path / "TEST-projects" / "TEST-other-session.jsonl", _person("u1", at(0), "TEST 别的会话。"))
    hook = _hook(client)
    try:
        hook.handle_payload(_stop(other))
        assert "capture_gap:session_record_unavailable" in hook.diagnostics.capability_gaps
        hook.handle_payload(_stop(tmp_path / "TEST-projects" / "missing" / "TEST-cc-session.jsonl"))
    finally:
        hook.close()
    assert _said_in_store(root) == []


def test_codex_does_not_read_a_session_record(store, tmp_path):
    root, _homes, _client, _capture = store
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    codex = tmp_path / "TEST-codex-home"
    attach_shared_record(
        root,
        client_entry_record(
            host="codex",
            home=codex,
            entry_id="codex",
            display_name="Codex",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    at = _moments()
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl", _person("u1", at(0), "TEST 不读。"))
    hook = CodexHookHandler.from_home(str(codex), "codex")
    try:
        hook.handle_payload({**_stop(record), "turn_id": "TEST-turn-1"})
    finally:
        hook.close()
    assert _rows(root, "SELECT count(*) FROM source_events WHERE entry_id='codex'") == [(0,)]


def test_a_task_notification_is_not_the_owner_s_prompt(store):
    """Claude Code hands the model a notice as a prompt when a background task finishes; on the pilot the
    first such notice was stored as the owner's message."""
    root, _homes, client, _capture = store
    notice = "<task-notification>\n<task-id>TEST</task-id>\n<status>completed</status>\n</task-notification>"
    hook = _hook(client)
    try:
        assert hook.handle_payload(_prompt(notice, prompt_id="TEST-prompt-9")) == {}
        assert hook.diagnostics.last_reason == "task_notification"
        hook.handle_payload(_prompt("TEST 一句真话。", prompt_id="TEST-prompt-10"))
    finally:
        hook.close()
    assert [content for _role, _origin, content in _said_in_store(root)] == ["TEST 一句真话。"]


SUGGESTIONS_PROMPT = (
    "# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with "
    "Codex in this local project: C:\\TEST\n\nGet an understanding of the user's intent and goals "
    "by deeply viewing their connected apps.\n\n# Rules\n\n"
    + "- TEST rule about what a suggestion must be and must not be.\n" * 120
    + "\n# Examples\n\n## Bad examples\n\n"
    + "- TEST bad example.\n" * 60
    + "\n# Response format\n\nJSON."
)


def test_codex_s_request_for_suggestions_is_told_from_the_owner_s_words():
    """It opens with a Markdown heading, names its hyperpersonalized suggestions early and runs to thousands of
    characters; a wording change around that still counts, the owner's own words about it do not."""
    from scope_recall.adapters.clients.boundary import is_codex_suggestions_prompt

    assert len(SUGGESTIONS_PROMPT) >= 8000 and is_codex_suggestions_prompt(SUGGESTIONS_PROMPT)
    reworded = SUGGESTIONS_PROMPT.replace("# Overview\n\nGenerate 0 to 3", "## Overview\n\nPropose up to three")
    assert is_codex_suggestions_prompt(reworded.replace("hyperpersonalized", "Hyperpersonalised"))
    assert not is_codex_suggestions_prompt("帮我看看 Codex 的 hyperpersonalized suggestions 是怎么生成的")
    assert not is_codex_suggestions_prompt("# 我的笔记\n\n" + "今天记下 hyperpersonalized suggestions 这个词。" * 5)
    assert not is_codex_suggestions_prompt("Codex 生成的建议如下。\n" + "hyperpersonalized suggestions\n" * 200)
    # The owner's own long notes about the feature, with a heading and even one of its section names, stay theirs.
    note = (
        "# 关于 Codex 的 hyperpersonalized suggestions\n\n## Overview\n\n"
        + "我在研究它每次发来的那段提示词，想弄清它为什么被当成我说的话存下来。\n" * 300
    )
    assert len(note) >= 8000 and not is_codex_suggestions_prompt(note)


def test_codex_s_request_for_suggestions_is_neither_stored_nor_recalled(store, tmp_path):
    """Codex sends its request for suggestions of what to do next through the prompt hook.  On the pilot four were
    stored as the owner's words, 11,000 to 15,000 characters each, claims were drawn from them as if the owner had
    said them, and nine of the model's JSON answers were stored as replies."""
    root, _homes, _client, _capture = store
    codex = _codex_client(root, tmp_path)
    hook = CodexHookHandler.from_home(str(codex), "codex")
    try:
        assert (
            hook.handle_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "TEST-codex-session",
                    "turn_id": "TEST-turn-1",
                    "prompt": SUGGESTIONS_PROMPT,
                    "cwd": "C:/TEST",
                }
            )
            == {}
        )
        assert hook.diagnostics.last_reason == "host_generated_prompt"
        assert (
            hook.handle_payload(
                {
                    "hook_event_name": "Stop",
                    "session_id": "TEST-codex-session",
                    "turn_id": "TEST-turn-1",
                    "cwd": "C:/TEST",
                    "last_assistant_message": '{"suggestions":[{"title":"TEST 建议"}]}',
                }
            )
            == {}
        )
        assert hook.diagnostics.last_reason == "host_generated_thread"
        # An answer in a thread whose request was not seen here is still told by what it is.
        assert (
            hook.handle_payload(
                {
                    "hook_event_name": "Stop",
                    "session_id": "TEST-unmarked-session",
                    "turn_id": "TEST-turn-1",
                    "cwd": "C:/TEST",
                    "last_assistant_message": '{"suggestions":[{"title":"TEST 建议"}]}',
                }
            )
            == {}
        )
        assert hook.diagnostics.last_reason == "host_generated_reply"
        hook.handle_payload(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-turn-2",
                "prompt": "TEST 一句真话。",
                "cwd": "C:/TEST",
            }
        )
        hook.handle_payload(
            {
                "hook_event_name": "Stop",
                "session_id": "TEST-codex-session",
                "turn_id": "TEST-turn-2",
                "cwd": "C:/TEST",
                "last_assistant_message": "TEST 好的。",
            }
        )
    finally:
        hook.close()
    stored = _rows(
        root,
        "SELECT content FROM source_events WHERE entry_id='codex' AND role IN ('user', 'assistant') ORDER BY rowid",
    )
    assert stored == [("TEST 一句真话。",), ("TEST 好的。",)]


def test_the_rest_of_codex_s_suggestions_thread_is_not_stored_either(store, tmp_path):
    """The request and its answer were kept out, but the thread's tool calls and end came through: on the pilot one
    thread left four tool outputs of 2-11 kB and an end marker.  Each hook is a process of its own, so the thread is
    marked when its request is recognized, and its end removes the mark."""
    root, _homes, _client, _capture = store
    codex = _codex_client(root, tmp_path)

    def hook(payload):
        handler = CodexHookHandler.from_home(str(codex), "codex")
        try:
            handler.handle_payload({"cwd": "C:/TEST", **payload})
            return handler.diagnostics.last_reason
        finally:
            handler.close()

    thread = {"session_id": "TEST-suggestions-thread", "turn_id": "TEST-turn-1"}
    tool = {
        "hook_event_name": "PostToolUse",
        "tool_name": "TEST-read",
        "tool_input": {"path": "C:/TEST/app.json"},
        "tool_response": "TEST 一份文件的内容。",
    }
    assert (
        hook({"hook_event_name": "UserPromptSubmit", **thread, "prompt": SUGGESTIONS_PROMPT}) == "host_generated_prompt"
    )
    assert hook({**tool, **thread, "tool_use_id": "TEST-tool-1"}) == "host_generated_thread"
    assert hook({"hook_event_name": "Interrupt", **thread}) == "host_generated_thread"
    assert (
        hook({"hook_event_name": "Stop", **thread, "last_assistant_message": "TEST 不是 JSON 的回答。"})
        == "host_generated_thread"
    )
    assert hook({"hook_event_name": "SessionEnd", **thread, "reason": "other"}) == "host_generated_thread"
    assert list((codex / "scope-recall" / "host-threads").iterdir()) == []
    hook({**tool, "session_id": "TEST-owner-thread", "turn_id": "TEST-turn-1", "tool_use_id": "TEST-tool-2"})
    stored = _rows(root, "SELECT role FROM source_events WHERE entry_id='codex'")
    assert stored == [("tool",)]


def test_a_prompt_longer_than_a_recall_query_is_still_recalled_for(store):
    """A recall request carries at most 8,192 characters of query.  A longer prompt went whole, the request was
    refused, and the turn had no recall at all: three of the work computer's Codex prompts in one morning."""
    _root, homes, client, _capture = store
    told = _hermes(homes["tianquan"])
    try:
        told.on_turn_start(1, "TEST 白鹭项目的负责人是 KZ-42。", turn_id="TEST-turn-1", session_id="TEST-session-1")
        told.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。"
        )
        told.sync_turn("TEST 白鹭项目的负责人是 KZ-42。", "好的。", session_id="TEST-session-1")
    finally:
        told.shutdown()
    # Distinct characters, so the prompt has far more than 128 distinct terms as a real one would.
    prompt = (
        "白鹭项目的负责人 KZ-42 是谁？下面是附件：\n"
        + "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈剑号巨阙珠称夜光果珍李柰菜重芥姜海咸河淡鳞潜羽翔龙师火帝鸟官人皇始制文字乃服衣裳推位让国有虞陶唐吊民伐罪周发殷汤坐朝问道垂拱平章爱育黎首臣伏戎羌遐迩一体率宾归王鸣凤在竹白驹食场化被草木赖及万方"
        * 3
        + "\n"
        + "TEST 附件里的一行字。\n" * 800
    )
    assert len(prompt) > 8192
    hook = _hook(client)
    try:
        result = hook.handle_payload(_prompt(prompt, prompt_id="TEST-prompt-long"))
    finally:
        hook.close()
    assert hook.diagnostics.last_reason != "recall_exception"
    # This store has no vector companion, and the hook says so where an operator can read it.
    assert hook.diagnostics.recall_vector_gap == "vector_unavailable"
    body = result["hookSpecificOutput"]["additionalContext"].partition("\n")[2]
    assert any("KZ-42" in item["content"] for item in json.loads(body)["items"])


def test_a_failed_recall_says_what_stopped_it(store, ample_budget, monkeypatch, capsys):
    """The work computer's server logged recall_exception three times with nothing else: the cause had to be found
    by reading the store.  The hook needs time enough to reach the recall: on the 2 s default a slow CI runner spent
    it attaching the runtime, and the hook said deadline_exceeded instead (windows-latest, 2026-10-05)."""
    from scope_recall.adapters.clients.hook_answer import emit_result
    from scope_recall.core import MemoryCore

    _root, _homes, client, _capture = store

    def refuse(self, *args, **kwargs):
        raise ContractError("STORAGE_UNAVAILABLE", "recall")

    monkeypatch.setattr(MemoryCore, "recall_packet", refuse)
    hook = _hook(client)
    try:
        assert hook.handle_payload(_prompt("TEST 这一问召回失败。", prompt_id="TEST-prompt-3")) == {}
    finally:
        hook.close()
    assert hook.diagnostics.last_reason == "recall_exception"
    assert hook.diagnostics.recall_error_detail == "ContractError:STORAGE_UNAVAILABLE"
    emit_result({}, diagnostics=hook.diagnostics)
    assert "CODEX_RECALL:ContractError:STORAGE_UNAVAILABLE" in capsys.readouterr().err


def test_a_prompt_the_store_could_not_take_is_still_recalled_by_meaning(store, monkeypatch):
    """The runtime, and with it the vector search, came only after a stored or queued capture.  A capture that
    failed left the turn to a recall by words alone: six prompts on the work computer's two entries in one night.
    A message refused as a credential still goes without it, so nothing of it reaches an embedding provider."""
    from scope_recall.core import MemoryCore

    _root, _homes, client, _capture = store

    def busy(self, *args, **kwargs):
        raise ContractError("STORAGE_UNAVAILABLE", "capture")

    hook = _hook(client)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(MemoryCore, "record_host_event", busy)
            hook.handle_payload(_prompt("TEST 存不进去的一句。", prompt_id="TEST-prompt-busy"))
        assert hook.diagnostics.capture_error_code == "STORAGE_UNAVAILABLE"
        assert hook._runtime_attach_attempted, "recalled with the vector search"
    finally:
        hook.close()
    refused = _hook(client)
    try:
        refused.handle_payload(_prompt("TEST password: Xk9#mP2q-7Lw", prompt_id="TEST-prompt-secret"))
        assert refused.diagnostics.capture_disposition == "rejected"
        assert not refused._runtime_attach_attempted, "a credential never reaches the embedding provider"
    finally:
        refused.close()


def test_a_prompt_whose_capture_failed_before_the_secret_screen_still_keeps_a_credential_from_the_vectors(
    store, monkeypatch
):
    """A capture that fails before it screens the message (an invalid envelope, say) is no refusal, and the vector
    search came with the runtime: the prompt itself is screened before the runtime is attached."""
    from scope_recall.core import MemoryCore

    _root, _homes, client, _capture = store

    def invalid(self, *args, **kwargs):
        raise ContractError("INPUT_INVALID", "capture")

    hook = _hook(client)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(MemoryCore, "record_host_event", invalid)
            hook.handle_payload(_prompt("TEST password: Xk9#mP2q-7Lw", prompt_id="TEST-prompt-invalid"))
        assert not hook._runtime_attach_attempted
    finally:
        hook.close()


def test_a_prompt_blank_for_its_first_8192_characters_is_recalled_by_what_follows(store):
    _root, homes, client, _capture = store
    told = _hermes(homes["tianquan"])
    try:
        told.on_turn_start(1, "TEST 白鹭项目的负责人是 KZ-42。", turn_id="TEST-turn-1", session_id="TEST-session-1")
        told.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。"
        )
        told.sync_turn("TEST 白鹭项目的负责人是 KZ-42。", "好的。", session_id="TEST-session-1")
    finally:
        told.shutdown()
    hook = _hook(client)
    try:
        result = hook.handle_payload(_prompt(" " * 9000 + "白鹭项目的负责人 KZ-42 是谁", prompt_id="TEST-prompt-blank"))
    finally:
        hook.close()
    assert hook.diagnostics.last_reason != "recall_exception", hook.diagnostics.recall_error_detail
    body = result["hookSpecificOutput"]["additionalContext"].partition("\n")[2]
    assert any("KZ-42" in item["content"] for item in json.loads(body)["items"])


def test_what_counts_as_a_recall_without_its_vector_search():
    """Only a search that did not run or did not finish; one that ran and had candidates refused did run."""
    from scope_recall.adapters.clients.hook_answer import recall_without_vectors

    assert recall_without_vectors(["vector_old_or_mismatched_space", "vector_rejected:space"]) is None
    assert recall_without_vectors(["sqlite_candidate_error:OperationalError"]) is None
    assert (
        recall_without_vectors(["vector_unavailable", "vector_error:TimeoutError:helper_open_deadline"])
        == "vector_error:TimeoutError:helper_open_deadline"
    )
    assert recall_without_vectors(["deadline_exceeded_collect"]) == "deadline_exceeded_collect"
    assert recall_without_vectors(["sqlite_unavailable:INPUT_INVALID"]) == "sqlite_unavailable:INPUT_INVALID"


def test_a_prompt_hook_starts_the_vector_helper_before_it_stores_the_prompt(monkeypatch):
    """Each hook is a new process, and a vector helper started when the recall reached its vector search spent the
    rest of the recall's budget importing LanceDB: Claude Code and Codex recalled from words alone."""
    from scope_recall.adapters.clients import hook_entry
    from scope_recall.vector import process_store

    started = []
    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: started.append(kwargs))
    monkeypatch.setattr(hook_entry.sys, "platform", "win32")
    hook_entry._prestart_vector_helper(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "TEST"}).encode())
    hook_entry._prestart_vector_helper(
        json.dumps({"hook_event_name": "Stop", "last_assistant_message": "UserPromptSubmit"}).encode()
    )
    hook_entry._prestart_vector_helper(b"not json")
    assert started == [{}]


@pytest.fixture
def resident(store, monkeypatch):
    """The Claude Code entry's MCP server answering its prompts' recall, without a LanceDB helper process."""
    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    root, _homes, client, _capture = store
    endpoint = local_endpoint.serve(client, "claude-code", warm=False)
    assert endpoint is not None
    try:
        yield root, client, endpoint
    finally:
        endpoint.stop()


@pytest.mark.skipif(os.name != "nt", reason="the LanceDB helper process is Windows'")
def test_the_mcp_server_shares_one_vector_store_among_its_recalls_before_it_serves(store, monkeypatch):
    """The kept handler, a handler made for a prompt that comes meanwhile and the tools search one store, through one
    LanceDB helper (``process_store.share``), from the first prompt it serves."""
    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    sharing_when_served = []

    class Server(local_endpoint._Server):
        def __init__(self, *args, **kwargs):
            sharing_when_served.append(process_store._sharing)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(local_endpoint, "_Server", Server)
    _root, _homes, client, _capture = store
    endpoint = local_endpoint.serve(client, "claude-code", warm=False)
    assert endpoint is not None
    try:
        assert sharing_when_served == [True]
    finally:
        endpoint.stop()


def _hook_entry(monkeypatch, raw, client):
    from scope_recall.adapters.clients import hook_entry

    monkeypatch.setattr(hook_entry.sys, "stdin", type("Stdin", (), {"buffer": __import__("io").BytesIO(raw)})())
    return hook_entry.main(["--home", str(client), "--host", "claude-code"])


def _counted(endpoint, monkeypatch, *, delay=0.0):
    """Count the recalls the server runs; ``delay`` keeps each one waiting that long first."""
    import time

    calls = []
    real = endpoint.recall

    def recall(request, **kwargs):
        calls.append(request["payload"].get("prompt"))
        time.sleep(delay)
        return real(request, **kwargs)

    monkeypatch.setattr(endpoint, "recall", recall)
    return calls


def _user_rows(root):
    return _rows(root, "SELECT content FROM source_events WHERE role='user' AND entry_id='claude-code'")


@pytest.fixture
def small_reserve(monkeypatch):
    """A test store's hooks run on the 2 s default budget (the pilot's entries have 6 s): keep back less of it for
    the hook's own recall, so the server answers before the hook recalls alongside."""
    from scope_recall.adapters.clients import handler as handler_module

    monkeypatch.setattr("scope_recall.adapters.clients.prompt_recall._LOCAL_RECALL_RESERVE_S", 0.3)
    monkeypatch.setattr("scope_recall.adapters.clients.prompt_recall._RESIDENT_MIN_S", 0.5)


@pytest.fixture
def ample_budget(monkeypatch):
    """For a test that needs the server's answer: a hook on the 2 s default asked no server when storing the prompt
    took most of it, as on a slow CI runner (the prompt's write took 5.6 s on windows-latest, 2026-10-04, and the hook
    recalled by itself).  The server answers at once; the time is only there to be enough."""
    from scope_recall.adapters.clients import handler as handler_module

    monkeypatch.setattr(handler_module, "_TOTAL_BUDGET_S", 30.0)


def test_the_server_answers_a_prompt_s_recall_and_the_hook_stores_the_prompt(
    resident, small_reserve, ample_budget, monkeypatch, capsys
):
    """A cold prompt's recall was often done before its LanceDB helper was ready: on the pilot 6 of 8 cold Claude
    Code prompts recalled by words alone.  The client's MCP server lives as long as the client and recalls warm; the
    prompt is stored by its own hook, as before."""
    root, client, endpoint = resident
    calls = _counted(endpoint, monkeypatch)
    raw = json.dumps(_prompt("TEST 常驻进程给这句召回。", prompt_id="TEST-prompt-resident")).encode()
    assert _hook_entry(monkeypatch, raw, client) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) is not None and "CODEX_RECALL_RESIDENT:answered" in captured.err
    assert calls == ["TEST 常驻进程给这句召回。"]
    assert _user_rows(root) == [("TEST 常驻进程给这句召回。",)]
    endpoint.stop()
    assert not endpoint.path.exists()


def test_a_late_answer_leaves_the_prompt_stored_once(resident, small_reserve, monkeypatch, capsys):
    """The first version had the server store the prompt too: one that answered after its hook stopped waiting left
    the prompt stored twice.  The server now only recalls, and the hook recalls itself; while its late recall runs, the
    server tells the next prompt at once that it is busy."""
    import time

    root, client, endpoint = resident
    calls = _counted(endpoint, monkeypatch, delay=3.0)
    raw = json.dumps(_prompt("TEST 常驻进程答得太晚。", prompt_id="TEST-prompt-late")).encode()
    assert _hook_entry(monkeypatch, raw, client) == 0
    assert "CODEX_RECALL_RESIDENT:late" in capsys.readouterr().err
    time.sleep(0.5)  # past the time the hook gave its server
    raw = json.dumps(_prompt("TEST 下一句不再等它。", prompt_id="TEST-prompt-after-late")).encode()
    assert _hook_entry(monkeypatch, raw, client) == 0
    assert "CODEX_RECALL_RESIDENT:busy" in capsys.readouterr().err and endpoint.path.exists()
    assert calls == ["TEST 常驻进程答得太晚。"], "later prompts go past it"
    time.sleep(3.0)  # the server's late recall ends
    assert sorted(_user_rows(root)) == [("TEST 下一句不再等它。",), ("TEST 常驻进程答得太晚。",)]


def test_only_a_prompt_that_may_use_vectors_asks_the_server(resident, monkeypatch, capsys):
    """The other hooks store what was said and read no vectors; a prompt holding a credential is recalled without
    the vector channel, so nothing of it reaches an embedding provider or the server."""
    root, client, endpoint = resident
    calls = _counted(endpoint, monkeypatch)
    stop = {
        "hook_event_name": "Stop",
        "session_id": "TEST-cc-session",
        "cwd": "C:/anywhere/at/all",
        "transcript_path": "C:/TEST/transcript.jsonl",
        "last_assistant_message": "TEST 自己保存的回复。",
    }
    assert _hook_entry(monkeypatch, json.dumps(stop).encode(), client) == 0
    secret = _prompt("TEST my password is Xk9#mP2qLm7", prompt_id="TEST-prompt-secret")
    assert _hook_entry(monkeypatch, json.dumps(secret).encode(), client) == 0
    assert calls == [] and "CODEX_RECALL_RESIDENT" not in capsys.readouterr().err


def test_a_name_whose_process_is_gone_reused_or_another_account_s_is_removed_unasked(store, monkeypatch):
    """A killed server leaves its name behind, and its port is free for any program to take.  On Windows a
    connection to a closed loopback port is refused only after 2 s, so a name was never removed that way."""
    from scope_recall._version import __version__
    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.runtime import process_probe

    _root, _homes, client, _capture = store
    folder = local_endpoint.endpoints(client)
    folder.mkdir(parents=True)
    names = {}
    for pid, version in ((111, __version__), (222, __version__), (333, __version__), (444, "0.0.1"), (555, "0.0.1")):
        names[pid] = folder / f"{pid}.json"
        names[pid].write_text(
            json.dumps(
                {"host": "claude-code", "port": 9, "token": "TEST", "pid": pid, "start": "1", "version": version}
            ),
            encoding="utf-8",
        )
    states = {
        111: process_probe.ProcessState(111, False),
        222: process_probe.ProcessState(222, True, "2"),
        333: process_probe.ProcessState(333, True, None),
        444: process_probe.ProcessState(444, False),
        555: process_probe.ProcessState(555, True, "1"),
    }
    monkeypatch.setattr(process_probe, "probe_process", lambda pid: states[pid])
    recaller = local_endpoint.Recaller(client, "claude-code")
    monkeypatch.setattr(recaller, "_exchange", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("asked")))
    assert recaller(_prompt("TEST 没人接。"), (), (), 3.0) is None and recaller.outcome == "none"
    assert [pid for pid, path in names.items() if path.exists()] == [555], "a live server of another version stays"


def test_a_program_on_a_server_s_port_learns_no_token_and_is_not_believed(store):
    """The hook sent the token and the prompt to whatever listened on the named port, and printed its answer."""
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from scope_recall._version import __version__
    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.runtime.process_probe import probe_process

    heard = []

    class Impostor(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            heard.append((self.path, dict(self.headers), body))
            answer = json.dumps({"result": {"hookSpecificOutput": "TEST injected"}, "diagnostics": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.send_header("X-Scope-Recall-Proof", "0" * 64)
            self.end_headers()
            self.wfile.write(answer)

    impostor = ThreadingHTTPServer(("127.0.0.1", 0), Impostor)
    threading.Thread(target=impostor.serve_forever, daemon=True).start()
    try:
        _root, _homes, client, _capture = store
        folder = local_endpoint.endpoints(client)
        folder.mkdir(parents=True)
        named = folder / "333.json"
        named.write_text(
            json.dumps(
                {
                    "host": "claude-code",
                    "port": impostor.server_address[1],
                    "token": "TEST-secret-token",
                    "pid": os.getpid(),
                    "start": probe_process(os.getpid()).start_token,
                    "version": __version__,
                }
            ),
            encoding="utf-8",
        )
        recaller = local_endpoint.Recaller(client, "claude-code")
        assert recaller(_prompt("TEST 不该被别的程序听到。"), (), (), 3.0) is None
        assert recaller.outcome == "unproven"
    finally:
        impostor.shutdown()
        impostor.server_close()
    assert [path for path, _headers, _body in heard] == ["/hello"], "nothing but the proof was asked of it"
    assert all(
        "TEST-secret-token" not in json.dumps(headers) and b"TEST-secret-token" not in body
        for _path, headers, body in heard
    )
    assert not named.exists()


def test_a_server_names_itself_again_only_once_no_recall_is_stuck(store, monkeypatch):
    """A server whose recall hung answered its own check anyway and named itself again, and every prompt then
    waited on it."""
    import time

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.setattr(local_endpoint, "ADVERTISE_SECONDS", 0.2)
    _root, _homes, client, _capture = store
    endpoint = local_endpoint.serve(client, "claude-code")
    try:
        with endpoint.lock:
            endpoint.inflight[1] = time.monotonic() - 1  # past the time its hook gave it
        endpoint.path.unlink()
        time.sleep(1.0)
        assert not endpoint.path.exists(), "not while a recall is stuck"
        with endpoint.lock:
            endpoint.inflight.clear()
        deadline = time.monotonic() + 5
        while not endpoint.path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert endpoint.path.exists()
    finally:
        endpoint.stop()


def test_a_busy_server_leaves_the_prompt_s_recall_to_its_hook(resident):
    from scope_recall.adapters.clients import local_endpoint

    _root, client, endpoint = resident
    for _slot in range(local_endpoint.MAX_CONCURRENT):
        assert endpoint.slots.acquire(blocking=False)
    recaller = local_endpoint.Recaller(client, "claude-code")
    assert recaller(_prompt("TEST 常驻进程正忙。"), (), (), 3.0) is None
    assert recaller.outcome == "busy" and endpoint.path.exists()


def test_a_server_answers_only_a_hook_that_proves_the_token(resident):
    import http.client

    _root, _client, endpoint = resident
    connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=5)
    try:
        connection.request(
            "POST", "/recall", body=b"{}", headers={"X-Scope-Recall-Nonce": "a" * 32, "X-Scope-Recall-Proof": "0" * 64}
        )
        assert connection.getresponse().status == 401
    finally:
        connection.close()


def test_a_long_prompt_s_recall_is_answered(resident, small_reserve, monkeypatch, capsys):
    """The first version re-escaped the payload into a JSON body: 22,000 Chinese characters made it too large (413)."""
    from scope_recall.adapters.clients import handler as handler_module

    # The budget an entry's config gives (the test store's default is 2 s): on a slow CI runner storing this prompt
    # took 2.25 s of the 2, and the hook never asked its server (rc13's CI).  The size is what is tested here.
    monkeypatch.setattr(handler_module, "_TOTAL_BUDGET_S", 10.0)
    root, client, endpoint = resident
    calls = _counted(endpoint, monkeypatch)
    raw = json.dumps(_prompt("TEST " + "长" * 20000, prompt_id="TEST-prompt-long"), ensure_ascii=False).encode("utf-8")
    assert len(raw) <= 65536
    assert _hook_entry(monkeypatch, raw, client) == 0
    assert "CODEX_RECALL_RESIDENT:answered" in capsys.readouterr().err and len(calls) == 1


def test_credentials_rotated_or_removed_in_the_env_file_are_taken_up(store, monkeypatch, tmp_path):
    """The server read its env file once: a rotated key stayed stale, and one taken out stayed in, until the client
    restarted."""
    import os
    import time

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.delenv("TEST_SCOPE_RECALL_KEY", raising=False)
    monkeypatch.delenv("TEST_SCOPE_RECALL_OLD", raising=False)
    _root, _homes, client, _capture = store
    env_file = tmp_path / "TEST.env"
    env_file.write_text("one\n", encoding="utf-8")
    loaded = {"TEST_SCOPE_RECALL_OLD": "old"}
    os.environ.update(loaded)  # what the server's start loaded
    endpoint = local_endpoint.serve(client, "claude-code", env_file=env_file, credentials=lambda: dict(loaded))
    try:
        loaded.clear()
        loaded["TEST_SCOPE_RECALL_KEY"] = "two"
        env_file.write_text("two, rotated\n", encoding="utf-8")
        request = {
            "payload": _prompt("TEST 换了密钥。", prompt_id="TEST-prompt-env"),
            "current_refs": (),
            "gaps": (),
            "remaining": 3.0,
        }
        _answer, close = endpoint.recall(request)
        close()
        assert os.environ.get("TEST_SCOPE_RECALL_KEY") == "two" and "TEST_SCOPE_RECALL_OLD" not in os.environ
    finally:
        endpoint.stop()
        os.environ.pop("TEST_SCOPE_RECALL_KEY", None)
        time.sleep(0)


def test_a_server_names_itself_in_the_user_s_own_profile(resident):
    """The entry's home may sit on a drive every account can read (on the pilot, F:\\ gives Authenticated Users
    write), and whoever holds a server's token can read the owner's memory through it."""
    import os
    import stat
    from pathlib import Path

    _root, client, endpoint = resident
    profile = Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else Path.home() / ".cache"
    assert profile in endpoint.path.parents and client not in endpoint.path.parents
    if os.name != "nt":
        assert stat.S_IMODE(endpoint.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(endpoint.path.parent.stat().st_mode) == 0o700


def test_a_prompt_with_half_of_a_broken_emoji_is_stored(store, capsys):
    """JavaScript writes half of a broken emoji as a lone surrogate (``\\ud83d``); a prompt holding one was refused
    whole as INPUT_INVALID and never stored."""
    root, _homes, client, _capture = store
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 表情坏了" + chr(0xD83D), prompt_id="TEST-prompt-surrogate"))
    finally:
        hook.close()
    assert ("user", "human_direct", "TEST 表情坏了" + chr(0xFFFD)) in _said_in_store(root)


def test_a_server_answer_that_ran_out_of_time_is_not_the_last_word(
    resident, small_reserve, ample_budget, monkeypatch, capsys
):
    """A server's recall that ended in deadline_exceeded or recall_exception was taken as final, though the hook had
    time for its own.  Then it was dropped and the hook said ``answered``: a server whose recalls kept failing looked
    healthy (review of rc11)."""
    _root, client, endpoint = resident
    marker = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "TEST-server-marker"}}
    for reason, detail, said in (
        ("deadline_exceeded", None, "failed:deadline_exceeded"),
        (
            "recall_exception",
            "ContractError:STORAGE_UNAVAILABLE",
            "failed:recall_exception:ContractError:STORAGE_UNAVAILABLE",
        ),
    ):
        monkeypatch.setattr(
            endpoint,
            "recall",
            lambda request, reason=reason, detail=detail, **kwargs: (
                {"result": marker, "diagnostics": {"last_reason": reason, "recall_error_detail": detail}},
                lambda: None,
            ),
        )
        raw = json.dumps(_prompt(f"TEST 服务器没来得及 {reason}。", prompt_id=f"TEST-prompt-{reason}")).encode()
        assert _hook_entry(monkeypatch, raw, client) == 0
        captured = capsys.readouterr()
        assert "TEST-server-marker" not in captured.out
        assert f"CODEX_RECALL_RESIDENT:{said}\n" in captured.err


def test_a_prompt_whose_server_runs_still_starts_its_helper(resident, small_reserve, monkeypatch):
    """A prompt whose server ran started no helper of its own, and every recall the hook then did itself (the server
    busy, late or failing) ran by words alone (review of rc11)."""
    from scope_recall.adapters.clients import hook_entry

    _root, client, _endpoint = resident
    started = []
    monkeypatch.setattr(hook_entry, "_prestart_vector_helper", started.append)
    raw = json.dumps(_prompt("TEST 仍然自己预热。", prompt_id="TEST-prompt-prestart")).encode()
    assert _hook_entry(monkeypatch, raw, client) == 0
    assert started == [raw]


def test_a_failed_read_of_the_env_file_keeps_the_keys(store, monkeypatch, tmp_path):
    """One failed read (a file locked just after a save) dropped the key for the rest of the session."""
    import os

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.delenv("TEST_SCOPE_RECALL_KEEP", raising=False)
    _root, _homes, client, _capture = store
    env_file = tmp_path / "TEST.env"
    env_file.write_text("one\n", encoding="utf-8")
    reads = {"fail": False}

    def credentials():
        if reads["fail"]:
            raise OSError("TEST locked")
        return {"TEST_SCOPE_RECALL_KEEP": "kept"}

    os.environ["TEST_SCOPE_RECALL_KEEP"] = "kept"
    endpoint = local_endpoint.serve(client, "claude-code", env_file=env_file, credentials=credentials)
    try:
        reads["fail"] = True
        env_file.write_text("one, saved\n", encoding="utf-8")
        endpoint._refresh_credentials()
        assert os.environ.get("TEST_SCOPE_RECALL_KEEP") == "kept"
        reads["fail"] = False
        endpoint._refresh_credentials()  # read again once it can be
        assert endpoint._env_seen == endpoint._env_stamp()
    finally:
        endpoint.stop()
        os.environ.pop("TEST_SCOPE_RECALL_KEEP", None)


def test_the_mcp_server_starts_whatever_its_endpoint_does(store, monkeypatch):
    from scope_recall.adapters.clients import local_endpoint

    _root, _homes, client, _capture = store
    monkeypatch.setattr(local_endpoint, "endpoints", lambda home: (_ for _ in ()).throw(RuntimeError("TEST no home")))
    assert local_endpoint.serve(client, "claude-code") is None


def test_a_slow_server_s_answer_is_taken_while_the_hook_recalls_alongside(resident, monkeypatch, capsys):
    """Given only what the hook kept back, a server's recall that needed most of the time had none, and neither had
    the hook's (review of rc11).  The server has all of it now: the hook recalls alongside once little is left, and
    takes the server's answer when it comes."""
    import time

    from scope_recall.adapters.clients import handler as handler_module

    _root, client, endpoint = resident
    monkeypatch.setattr(
        "scope_recall.adapters.clients.prompt_recall._LOCAL_RECALL_RESERVE_S", 5.0
    )  # alongside from the start
    monkeypatch.setattr("scope_recall.adapters.clients.prompt_recall._RESIDENT_MIN_S", 0.5)
    marker = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "TEST-server-marker"}}

    def slow(request, **kwargs):
        time.sleep(0.6)
        return {"result": marker, "diagnostics": {}}, (lambda: None)

    def own(self, *args, **kwargs):
        time.sleep(1.2)  # still recalling when the server answers
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "TEST-own-marker"}}

    monkeypatch.setattr(endpoint, "recall", slow)
    monkeypatch.setattr("scope_recall.adapters.clients.prompt_recall.PromptRecall.own", own)
    raw = json.dumps(_prompt("TEST 慢的服务器也算数。", prompt_id="TEST-prompt-slow")).encode()
    assert _hook_entry(monkeypatch, raw, client) == 0
    captured = capsys.readouterr()
    assert "TEST-server-marker" in captured.out and "CODEX_RECALL_RESIDENT:answered" in captured.err


def test_a_server_whose_recall_is_past_its_time_sends_hooks_on(resident, monkeypatch):
    """A recall's time counted 2 s past what its hook gave it, and in between a hung server answered, named itself
    again, and the next prompt waited on it (review of rc11).  It is stuck from that time, and until the recall ends
    it tells every hook at once that it is busy."""
    import time

    from scope_recall.adapters.clients import local_endpoint

    _root, client, endpoint = resident
    seen = []

    def recall(request, **kwargs):
        time.sleep(request["remaining"] + 0.6)
        seen.append(endpoint._stuck())
        return {"result": {}, "diagnostics": {}}, (lambda: None)

    monkeypatch.setattr(endpoint, "recall", recall)
    assert local_endpoint.Recaller(client, "claude-code")(_prompt("TEST 超时的召回。"), (), (), 1.5) is None
    time.sleep(0.8)  # the recall above ends
    assert seen == [True], "stuck from the time its hook gave it"
    assert endpoint.path.exists(), "a late server keeps its name: it says it is busy while its recall is stuck"
    with endpoint.lock:
        endpoint.inflight[1] = time.monotonic() - 0.01
    recaller = local_endpoint.Recaller(client, "claude-code")
    assert recaller(_prompt("TEST 它还卡着。"), (), (), 3.0) is None
    assert recaller.outcome == "busy" and endpoint.path.exists(), "a busy server keeps its name"
    with endpoint.lock:
        endpoint.inflight.clear()
    monkeypatch.setattr(endpoint, "recall", lambda request, **kwargs: ({"result": {}, "diagnostics": {}}, lambda: None))
    recaller = local_endpoint.Recaller(client, "claude-code")
    assert recaller(_prompt("TEST 它好了。"), (), (), 3.0) is not None and recaller.outcome == "answered"


def test_a_hung_first_name_leaves_time_for_the_next(resident, monkeypatch):
    """A name whose server did not prove itself took the whole second of finding, and the next was never asked
    (review of rc11)."""
    import os
    import socket
    import time

    from scope_recall._version import __version__
    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.runtime.process_probe import probe_process

    _root, client, endpoint = resident
    monkeypatch.setattr(endpoint, "recall", lambda request, **kwargs: ({"result": {}, "diagnostics": {}}, lambda: None))
    with closing(socket.socket()) as hung:
        hung.bind(("127.0.0.1", 0))
        hung.listen(8)  # takes the connection and never answers
        named = endpoint.path.parent / "1.json"
        named.write_text(
            json.dumps(
                {
                    "host": "claude-code",
                    "port": hung.getsockname()[1],
                    "token": "TEST",
                    "pid": os.getpid(),
                    "start": probe_process(os.getpid()).start_token,
                    "version": __version__,
                }
            ),
            encoding="utf-8",
        )
        newer = time.time() + 60
        os.utime(named, (newer, newer))  # the newest name, asked first
        recaller = local_endpoint.Recaller(client, "claude-code")
        assert recaller(_prompt("TEST 第一个不应答。"), (), (), 3.0) is not None
    # Kept, a name whose process no longer answered cost every later prompt its wait (review of rc11).
    assert recaller.outcome == "answered" and not named.exists()


def test_a_first_read_of_the_env_file_that_failed_is_tried_again(store, monkeypatch, tmp_path):
    """A server that could not read its env file at its start recorded it as read, and recalled by words alone until
    its client restarted (review of rc11)."""
    import os

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.delenv("TEST_SCOPE_RECALL_LATE", raising=False)
    _root, _homes, client, _capture = store
    env_file = tmp_path / "TEST.env"
    env_file.write_text("one\n", encoding="utf-8")
    reads = {"fail": True}

    def credentials():
        if reads["fail"]:
            raise OSError("TEST locked")
        return {"TEST_SCOPE_RECALL_LATE": "late"}

    endpoint = local_endpoint.serve(client, "claude-code", env_file=env_file, credentials=credentials)
    try:
        reads["fail"] = False
        endpoint._refresh_credentials()  # the next prompt, the file unchanged
        assert os.environ.get("TEST_SCOPE_RECALL_LATE") == "late"
    finally:
        endpoint.stop()
        os.environ.pop("TEST_SCOPE_RECALL_LATE", None)


def test_a_key_the_runtime_config_comes_to_name_is_taken_up(store, monkeypatch, tmp_path):
    """Only the env file was watched: a runtime config that came to name another key was not read again, and a key
    the server's own start could not read stayed out though its endpoint had read it (review of rc11)."""
    import os

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    names = ("TEST_SCOPE_RECALL_A", "TEST_SCOPE_RECALL_B")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    _root, _homes, client, _capture = store
    env_file, runtime_config = tmp_path / "TEST.env", tmp_path / "TEST-runtime-config.json"
    env_file.write_text("A and B\n", encoding="utf-8")
    runtime_config.write_text('{"TEST": "names A"}', encoding="utf-8")
    declared = {"TEST_SCOPE_RECALL_A": "a"}
    endpoint = local_endpoint.serve(
        client, "claude-code", env_file=env_file, runtime_config=runtime_config, credentials=lambda: dict(declared)
    )
    try:
        assert os.environ.get("TEST_SCOPE_RECALL_A") == "a"
        declared.clear()
        declared["TEST_SCOPE_RECALL_B"] = "b"
        runtime_config.write_text('{"TEST": "names B instead"}', encoding="utf-8")
        endpoint._refresh_credentials()
        assert os.environ.get("TEST_SCOPE_RECALL_B") == "b" and "TEST_SCOPE_RECALL_A" not in os.environ
    finally:
        endpoint.stop()
        for name in names:
            os.environ.pop(name, None)


def test_a_payload_nested_past_the_interpreter_s_limit_is_cleaned():
    """The cleaning walked a payload by calling itself: a tool's output nested 499 levels deep ended the hook with a
    RecursionError on Python 3.11 (review of rc11)."""
    from scope_recall.adapters.clients.boundary import without_lone_surrogates

    top = node = {}
    for _level in range(5000):
        node["x"] = [{}]
        node = node["x"][0]
    node["text"] = "TEST" + chr(0xD83D)
    cleaned = without_lone_surrogates({"tool_response": top, "k" + chr(0xDC00): [chr(0xD800), 1, None]})
    node = cleaned["tool_response"]
    for _level in range(5000):
        node = node["x"][0]
    assert node["text"] == "TEST" + chr(0xFFFD)
    assert cleaned["k" + chr(0xFFFD)] == [chr(0xFFFD), 1, None]


def _marker(text):
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def test_a_server_answer_without_its_vector_search_gives_way_to_the_hook_s_own(
    resident, small_reserve, ample_budget, monkeypatch, capsys
):
    """A server whose vector search failed on its own (its key lost, say) answered every prompt by words alone, and
    the hook took that though it had the key, the helper and the time (review of rc11).  The hook's own recall is
    used when it has its vector search, and the server's words when it has not either.  A provider's refusal the hook
    would meet as well: that answer is used as it is, and the hook does not recall a second time."""
    from scope_recall.adapters.clients import handler as handler_module

    _root, client, endpoint = resident
    own_fault = "vector_error:AuxiliaryModelError:credential_missing"
    monkeypatch.setattr(handler_module.CodexHookHandler, "has_vectors", property(lambda self: True))
    for index, (gap, own_vectors, expected, said) in enumerate(
        (
            (own_fault, True, "TEST-own-marker", f"without_vectors:{own_fault}"),
            (own_fault, False, "TEST-server-marker", "answered"),
            ("vector_error:AuxiliaryModelError:http_status:429", True, "TEST-server-marker", "answered"),
            # A hook with no vector search of its own takes the server's as it is.
            ("vector_error:AuxiliaryModelError:credential_missing", None, "TEST-server-marker", "answered"),
        )
    ):
        monkeypatch.setattr(
            endpoint,
            "recall",
            lambda request, gap=gap, **kwargs: (
                {
                    "result": _marker("TEST-server-marker"),
                    "diagnostics": {"recall_vectors": False, "recall_vector_gap": gap},
                },
                lambda: None,
            ),
        )
        own_calls = []

        def recall(self, *args, own_vectors=own_vectors, own_calls=own_calls, **kwargs):
            own_calls.append(1)
            self._hook.diagnostics.recall_vectors = own_vectors
            return _marker("TEST-own-marker")

        monkeypatch.setattr("scope_recall.adapters.clients.prompt_recall.PromptRecall.own", recall)
        monkeypatch.setattr(
            handler_module.CodexHookHandler, "has_vectors", property(lambda self, route=own_vectors is not None: route)
        )
        raw = json.dumps(_prompt(f"TEST 服务器没有向量 {index}。", prompt_id=f"TEST-prompt-v-{index}")).encode()
        assert _hook_entry(monkeypatch, raw, client) == 0
        captured = capsys.readouterr()
        assert expected in captured.out and f"CODEX_RECALL_RESIDENT:{said}\n" in captured.err
        assert own_calls == ([] if "http_status" in gap or own_vectors is None else [1]), (
            "a provider's refusal is not recalled again, nor by a hook with no vector search"
        )


def test_a_hook_whose_own_recall_went_without_vectors_waits_for_its_server(resident, monkeypatch, capsys):
    """Recalling alongside, the hook stopped waiting the moment its own recall was done, dropped an answer that came in
    the time it had given the server, and called the server late (review of rc11).  A hook whose own recall had no
    vector search waits until its own time is up; one whose own had it does not."""
    import time

    from scope_recall.adapters.clients import handler as handler_module

    _root, client, endpoint = resident
    monkeypatch.setattr(
        "scope_recall.adapters.clients.prompt_recall._LOCAL_RECALL_RESERVE_S", 5.0
    )  # alongside from the start
    monkeypatch.setattr("scope_recall.adapters.clients.prompt_recall._RESIDENT_MIN_S", 0.5)

    def slow(request, **kwargs):
        time.sleep(0.8)
        return {"result": _marker("TEST-server-marker"), "diagnostics": {"recall_vectors": True}}, (lambda: None)

    monkeypatch.setattr(endpoint, "recall", slow)
    for own_vectors, expected, said in ((False, "TEST-server-marker", "answered"), (True, "TEST-own-marker", "slow")):

        def recall(self, *args, own_vectors=own_vectors, **kwargs):
            self._hook.diagnostics.recall_vectors = own_vectors
            if not own_vectors:
                self._hook.note("deadline_exceeded")  # what the hook's own said, which is not what answered
            return _marker("TEST-own-marker")

        monkeypatch.setattr("scope_recall.adapters.clients.prompt_recall.PromptRecall.own", recall)
        raw = json.dumps(
            _prompt(f"TEST 等一等服务器 {own_vectors}。", prompt_id=f"TEST-prompt-w-{own_vectors}")
        ).encode()
        assert _hook_entry(monkeypatch, raw, client) == 0
        captured = capsys.readouterr()
        assert expected in captured.out and f"CODEX_RECALL_RESIDENT:{said}\n" in captured.err
        assert "CODEX_HOOK:deadline_exceeded" not in captured.err
        time.sleep(1.0)  # the server's recall ends


def test_a_server_whose_recall_raises_says_so_and_keeps_its_name(resident, monkeypatch, capsys):
    """A recall that raised dropped the connection: the hook took the server for another program and removed its
    name, which came back and failed the same way, with only the error's class in the log (review of rc11)."""
    from scope_recall.adapters.clients import local_endpoint

    _root, client, endpoint = resident

    def broken(request, **kwargs):
        raise KeyError("TEST broken")

    monkeypatch.setattr(endpoint, "recall", broken)
    recaller = local_endpoint.Recaller(client, "claude-code")
    answered = recaller(_prompt("TEST 服务器出错。"), (), (), 3.0)
    assert answered == ({}, {"last_reason": "recall_exception", "recall_error_detail": "KeyError"})
    assert recaller.outcome == "answered" and endpoint.path.exists()
    err = capsys.readouterr().err
    assert "SCOPE_RECALL_ENDPOINT:recall_failed" in err and "Traceback" in err


def test_a_runtime_config_that_will_not_load_keeps_the_keys(store, monkeypatch, tmp_path):
    """Read again whenever the runtime config changed, a config that did not load (a TypeError) raised out of the
    server's recall (review of rc11); what is loaded stays, and it is read again at the next prompt."""
    import os

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.delenv("TEST_SCOPE_RECALL_STAYS", raising=False)
    _root, _homes, client, _capture = store
    env_file, runtime_config = tmp_path / "TEST.env", tmp_path / "TEST-runtime-config.json"
    env_file.write_text("one\n", encoding="utf-8")
    runtime_config.write_text("{}", encoding="utf-8")
    reads = {"fail": False}

    def credentials():
        if reads["fail"]:
            raise TypeError("TEST scope_ids")
        return {"TEST_SCOPE_RECALL_STAYS": "kept"}

    endpoint = local_endpoint.serve(
        client, "claude-code", env_file=env_file, runtime_config=runtime_config, credentials=credentials
    )
    try:
        reads["fail"] = True
        runtime_config.write_text('{"scope_ids": 5}', encoding="utf-8")
        endpoint._refresh_credentials()
        assert os.environ.get("TEST_SCOPE_RECALL_STAYS") == "kept"
        assert endpoint._env_seen != endpoint._env_stamp(), "read again at the next prompt"
    finally:
        endpoint.stop()
        os.environ.pop("TEST_SCOPE_RECALL_STAYS", None)


def test_a_payload_nested_past_the_parser_s_limit_is_answered_empty(store, monkeypatch, capsys, past_the_parser):
    """Nested past what the JSON parser takes, a tool's output ended the hook with a RecursionError and no answer
    (review of rc11)."""
    _root, _homes, client, _capture = store
    raw = past_the_parser(b'{"hook_event_name": "PostToolUse", "tool_response": ', b"}")
    assert _hook_entry(monkeypatch, raw, client) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_a_server_answer_from_an_unreadable_store_gives_way_to_the_hook_s_own(
    resident, small_reserve, ample_budget, monkeypatch, capsys
):
    """A server whose store could not be read answered with an empty packet, which read as nothing found and was
    taken over the hook's own recall (review of rc11)."""
    from scope_recall.adapters.clients import handler as handler_module

    _root, client, endpoint = resident
    monkeypatch.setattr(
        endpoint,
        "recall",
        lambda request, **kwargs: (
            {
                "result": {},
                "diagnostics": {
                    "last_reason": "recall_incomplete",
                    "recall_error_detail": "sqlite_unavailable:DatabaseError",
                },
            },
            lambda: None,
        ),
    )
    monkeypatch.setattr(
        "scope_recall.adapters.clients.prompt_recall.PromptRecall.own",
        lambda self, *args, **kwargs: _marker("TEST-own-marker"),
    )
    raw = json.dumps(_prompt("TEST 库读不到。", prompt_id="TEST-prompt-unreadable")).encode()
    assert _hook_entry(monkeypatch, raw, client) == 0
    captured = capsys.readouterr()
    assert "TEST-own-marker" in captured.out
    assert "CODEX_RECALL_RESIDENT:failed:recall_incomplete:sqlite_unavailable:DatabaseError\n" in captured.err


def test_a_hook_knows_whether_its_own_runtime_has_a_vector_search(store):
    """Nothing tested what a hook took its own vector route from (review of rc11)."""
    from types import SimpleNamespace

    _root, _homes, client, _capture = store
    hook = _hook(client)
    try:
        for runtime, expected in (
            (None, False),
            (SimpleNamespace(runtime=None), False),
            (SimpleNamespace(runtime=SimpleNamespace(config=SimpleNamespace(vector=None))), False),
            (SimpleNamespace(runtime=SimpleNamespace(config=SimpleNamespace(vector=object()))), True),
        ):
            hook._host_runtime = runtime
            assert hook.has_vectors is expected
    finally:
        hook._host_runtime = None
        hook.close()


def test_a_request_the_server_cannot_read_is_refused_and_its_name_kept(resident, monkeypatch, past_the_parser):
    """A payload nested past what the server's parser takes was answered 400, which the hook took for another program
    on the port, and it removed the server's name (review of rc11)."""
    import hashlib
    import http.client

    from scope_recall.adapters.clients import local_endpoint

    _root, client, endpoint = resident
    body = past_the_parser(b'{"payload": ', b', "current_refs": [], "gaps": [], "remaining": 1.0}')
    nonce = "a" * 32
    connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/recall",
            body=body,
            headers={
                "X-Scope-Recall-Nonce": nonce,
                "X-Scope-Recall-Proof": local_endpoint._proof(
                    endpoint.token, "recall", nonce, hashlib.sha256(body).hexdigest()
                ),
            },
        )
        assert connection.getresponse().status == 400
    finally:
        connection.close()

    def unreadable(body):
        raise ValueError("TEST unreadable")

    monkeypatch.setattr(local_endpoint, "_request", unreadable)
    recaller = local_endpoint.Recaller(client, "claude-code")
    assert recaller(_prompt("TEST 读不懂的请求。"), (), (), 3.0) is None
    assert recaller.outcome == "refused" and endpoint.path.exists()


def test_a_removed_name_comes_back_within_seconds(resident):
    """A server that did not prove itself in time, being busy, was left out for 30 s (review of rc11)."""
    import time

    _root, _client, endpoint = resident
    endpoint.path.unlink()
    deadline = time.monotonic() + 6
    while not endpoint.path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert endpoint.path.exists()


def test_a_first_read_of_the_key_that_raised_does_not_stop_the_server(store, monkeypatch, tmp_path):
    """Only an OSError or a ValueError was caught at the server's first read of its key: another error kept the
    endpoint from starting at all (review of rc11)."""
    import os

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.delenv("TEST_SCOPE_RECALL_FIRST", raising=False)
    _root, _homes, client, _capture = store
    env_file = tmp_path / "TEST.env"
    env_file.write_text("one\n", encoding="utf-8")
    reads = {"fail": True}

    def credentials():
        if reads["fail"]:
            raise TypeError("TEST config")
        return {"TEST_SCOPE_RECALL_FIRST": "first"}

    endpoint = local_endpoint.serve(client, "claude-code", env_file=env_file, credentials=credentials)
    assert endpoint is not None
    try:
        reads["fail"] = False
        endpoint._refresh_credentials()
        assert os.environ.get("TEST_SCOPE_RECALL_FIRST") == "first"
    finally:
        endpoint.stop()
        os.environ.pop("TEST_SCOPE_RECALL_FIRST", None)


def test_a_session_record_line_or_reply_nested_past_the_parser_s_limit_is_passed_over(tmp_path, past_the_parser):
    """A record line nested past what the parser takes ended every later Stop of the session, and so did a Codex
    reply of that shape (review of rc11)."""
    from scope_recall.adapters.clients import transcript
    from scope_recall.adapters.clients.boundary import is_codex_suggestions_reply

    record = tmp_path / "TEST-record.jsonl"
    line = past_the_parser(b"", b"\n")
    record.write_bytes(line)
    assert [(end, said) for end, said in transcript.read(record, 0, limit=65536)] == [(len(line), None)]
    assert is_codex_suggestions_reply(past_the_parser(b'{"suggestions": ', b"}").decode("ascii")) is False


def test_which_vector_faults_are_the_server_s_own():
    """A server's own fault makes the hook recall a second time; one the hook meets as well only cost the prompt its
    time and a second metered call.  The lists missed faults both ways (reviews of rc11).  Since the server keeps its
    embedding connection and worker between prompts, their failures are its own (review of rc12)."""
    from scope_recall.adapters.clients.hook_answer import server_own_vector_fault

    for gap in (
        "vector_unavailable",
        "vector_error:AuxiliaryModelError:credential_missing",
        "vector_error:AuxiliaryModelError:credential_shape_invalid",
        "vector_error:AuxiliaryModelError:network_error",
        "vector_error:AuxiliaryModelError:http_protocol",
        "vector_error:AuxiliaryModelError:transport_unavailable",
        "vector_error:AuxiliaryModelError:transport_worker",
        "vector_error:AuxiliaryModelError:transport_worker_protocol",
        "vector_error:RuntimeError:helper_lock_timeout",
        "vector_error:RuntimeError:worker_not_running",
        "vector_error:RuntimeError:table_not_open",
        "vector_error:RuntimeError",
        "vector_error:MemoryError",
    ):
        assert server_own_vector_fault(gap), gap
    shared = (
        "http_status:429",
        "timeout",
        "provider_hold",
        "model_refused",
        "request_rejected",
        "request_limit",
        "request_invalid",
        "response_limit",
        "response_status_failed",
        "budget_exhausted",
        "budget_unavailable",
        "meter_breach",
        "invalid_json",
        "empty_output",
        "missing_usage",
        "input_invalid",
        "sensitive_request",
        "endpoint_invalid",
        "unsupported_response_shape",
        "unicode_error",
        "vector_dimension_mismatch",
        "vector_nonfinite",
        "vector_zero",
        "http_redirect",
    )
    for gap in (
        *(f"vector_error:AuxiliaryModelError:{kind}" for kind in shared),
        "vector_error:AuxiliaryModelError",
        "deadline_exceeded_vector",
        "vector_error:",
        None,
        "",
    ):
        assert not server_own_vector_fault(gap), gap


def test_a_packet_emptied_because_its_read_did_not_finish_is_incomplete():
    """Only a packet whose store was unreadable or whose time was gone before the read was taken for incomplete; one
    whose time ran out later (collecting, compiling, releasing) came back empty as if nothing were found, and a server's
    such answer was taken over the hook's own (review of rc11)."""
    from scope_recall.adapters.clients.hook_answer import recall_incomplete

    assert (
        recall_incomplete({"status": "unavailable", "gaps": ["vector_unavailable", "deadline_exceeded_release_fence"]})
        == "deadline_exceeded_release_fence"
    )
    assert (
        recall_incomplete({"status": "unavailable", "gaps": ["sqlite_unavailable:DatabaseError"]})
        == "sqlite_unavailable:DatabaseError"
    )
    assert recall_incomplete({"status": "unavailable", "gaps": ["authority_TEST"]}) == "authority_TEST"
    assert recall_incomplete({"status": "unavailable", "gaps": []}) == "unavailable"
    for status in ("ok", "partial", "no_match"):
        assert recall_incomplete({"status": status, "gaps": ["deadline_exceeded_collect"]}) is None


def test_a_server_busy_for_less_than_a_hook_s_wait_is_answered(resident, monkeypatch):
    """At 0.3 s, a server busy with other recalls did not prove itself in time and lost its name (review of rc11)."""
    import time

    from scope_recall.adapters.clients import local_endpoint

    _root, client, endpoint = resident

    def slow(*args, **kwargs):
        time.sleep(0.4)  # what several recalls at once cost a proof on Windows
        return False

    monkeypatch.setattr(endpoint, "_stuck", slow)
    monkeypatch.setattr(endpoint, "recall", lambda request, **kwargs: ({"result": {}, "diagnostics": {}}, lambda: None))
    recaller = local_endpoint.Recaller(client, "claude-code")
    assert recaller(_prompt("TEST 服务器有点忙。"), (), (), 3.0) is not None
    assert recaller.outcome == "answered" and endpoint.path.exists()


def test_a_server_names_itself_again_only_when_its_check_comes_back_in_time(store, monkeypatch):
    """Checked from inside the server, a hello slowed by its own busy threads passed, and a server hooks could not
    reach in time named itself again; held to a hook's 0.5 s, the check kept out a server hooks reached (reviews of
    rc11).  A check that takes longer than a hook's wait but within its own allowance (0.75 s) names it again; one of
    0.9 s does not."""
    import time

    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    monkeypatch.setattr(local_endpoint, "ADVERTISE_SECONDS", 0.2)
    real = local_endpoint._hello

    def slow(connection, token):
        time.sleep(0.9)  # over the check's 0.75 s, under the 1 s it was once allowed
        return real(connection, token)

    connect = local_endpoint.http.client.HTTPConnection.connect

    def slow_connect(self):
        time.sleep(0.9)  # over the check's 0.75 s, under the 1 s it was once allowed
        return connect(self)

    _root, _homes, client, _capture = store
    endpoint = local_endpoint.serve(client, "claude-code")
    try:
        for name, owner, stand_in in (
            ("connect", local_endpoint.http.client.HTTPConnection, slow_connect),
            ("_hello", local_endpoint, slow),
        ):
            with monkeypatch.context() as patched:
                patched.setattr(owner, name, stand_in)
                endpoint.path.unlink(missing_ok=True)
                time.sleep(2.0)
                assert not endpoint.path.exists(), f"not while its {name} is slower than its check allows"

        def busy(connection, token):
            time.sleep(local_endpoint.PROOF_SECONDS + 0.1)  # over a hook's wait, within the check's allowance
            return real(connection, token)

        monkeypatch.setattr(local_endpoint, "_hello", busy)
        deadline = time.monotonic() + 5
        while not endpoint.path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert endpoint.path.exists()
    finally:
        endpoint.stop()


def test_a_server_s_traceback_keeps_the_frame_that_raised(resident, monkeypatch, capsys):
    """The server's traceback kept its outer frames and dropped the one that raised (review of rc11)."""
    from scope_recall.adapters.clients import local_endpoint

    _root, client, endpoint = resident

    def dig(depth):
        if depth == 0:
            raise KeyError("TEST deep")
        dig(depth - 1)

    def broken(request, **kwargs):
        dig(12)

    monkeypatch.setattr(endpoint, "recall", broken)
    assert local_endpoint.Recaller(client, "claude-code")(_prompt("TEST 很深的错误。"), (), (), 3.0) is not None
    assert 'raise KeyError("TEST deep")' in capsys.readouterr().err


def test_a_refused_request_is_not_sent_to_the_next_server(resident, monkeypatch):
    """A request one server could not read was sent to the next, which reads it no differently (review of rc11)."""
    import os
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from scope_recall._version import __version__
    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.runtime.process_probe import probe_process

    _root, client, endpoint = resident
    heard = []

    class Other(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            heard.append(self.path)
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

    other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        named = endpoint.path.parent / "1.json"
        named.write_text(
            json.dumps(
                {
                    "host": "claude-code",
                    "port": other.server_address[1],
                    "token": "TEST",
                    "pid": os.getpid(),
                    "start": probe_process(os.getpid()).start_token,
                    "version": __version__,
                }
            ),
            encoding="utf-8",
        )
        older = time.time() - 60
        os.utime(named, (older, older))  # asked second

        def unreadable(body):
            raise ValueError("TEST unreadable")

        monkeypatch.setattr(local_endpoint, "_request", unreadable)
        recaller = local_endpoint.Recaller(client, "claude-code")
        assert recaller(_prompt("TEST 读不懂的请求。"), (), (), 3.0) is None and recaller.outcome == "refused"
        assert heard == [], "the next server is not asked"
    finally:
        other.shutdown()
        other.server_close()


class _KeptFake:
    """A handler as the kept recaller uses it: counted when made, closed, and told what to answer."""

    made: list = []

    def __init__(self, *, fail=False, attach_failed=False):
        from scope_recall.adapters.clients.hook_answer import HookDiagnostics

        self.diagnostics = HookDiagnostics(capability_gaps=("TEST-gap",))
        self.fail, self.runtime_ready, self.closed, self.calls = fail, not attach_failed, False, []
        _KeptFake.made.append(self)

    def resident_recall_for(self, payload, current_refs, gaps, remaining):
        self.calls.append(remaining)
        if self.fail:
            raise RuntimeError("TEST recall raised")
        self.diagnostics.recall_vectors = True
        return {"TEST": payload["prompt"]}

    def close(self):
        self.closed = True


def test_a_kept_recaller_uses_one_handler_until_its_files_change():
    """A server made a handler for each prompt's recall and opened its vector table and embedding worker each time:
    3.9-4.1 s a recall, two of five without their vector search; kept, 1.6-2.1 s with it (rc12).  It is made anew
    once the files it was made with change, and the one before is closed."""
    from scope_recall.adapters.clients.local_endpoint import KeptRecaller

    _KeptFake.made = []
    stamp = ["one"]
    kept = KeptRecaller(_KeptFake, stamp=lambda: stamp[0])
    answers = [kept(_prompt(f"TEST {index}"), (), (), 5.0) for index in range(3)]
    assert len(_KeptFake.made) == 1 and [answer[0] for answer in answers] == [{"TEST": f"TEST {i}"} for i in range(3)]
    assert answers[0][1]["recall_vectors"] is True and answers[0][1]["capability_gaps"] == ["TEST-gap"]
    stamp[0] = "two"
    kept(_prompt("TEST after"), (), (), 5.0)
    assert len(_KeptFake.made) == 2 and _eventually(lambda: _KeptFake.made[0].closed) and not _KeptFake.made[1].closed
    kept.close()
    assert _KeptFake.made[1].closed and kept(_prompt("TEST closed"), (), (), 5.0) is None


def test_a_kept_recaller_counts_its_time_from_the_request_s_arrival():
    import time

    from scope_recall.adapters.clients.local_endpoint import KeptRecaller

    _KeptFake.made = []
    kept = KeptRecaller(_KeptFake)
    kept(_prompt("TEST late"), (), (), 5.0, received=time.monotonic() - 2.0)
    assert 2.9 < _KeptFake.made[0].calls[0] <= 3.0


class _WarmedFake(_KeptFake):
    """A kept handler that counts the searches of its vector store a server makes off any prompt's time."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.warmed = 0

    def warm_vectors(self, seconds):
        self.warmed += 1


def test_a_kept_recaller_searches_its_vector_store_again_after_an_idle_stretch(monkeypatch):
    """Every search reads the whole index, and left alone the OS gave its pages to other work: the first recall after
    an idle hour searched past its time (2 of 4 on this machine's Claude Code, 2026-10-02).  The server searches once
    more after each idle stretch; a recall starts the stretch again, and a closed recaller searches no more."""
    import time

    from scope_recall.adapters.clients import local_endpoint

    monkeypatch.setattr(local_endpoint, "KEEP_WARM_IDLE_SECONDS", 1.0)
    monkeypatch.setattr(local_endpoint, "KEEP_WARM_CHECK_SECONDS", 0.05)
    _KeptFake.made = []
    kept = local_endpoint.KeptRecaller(_WarmedFake)
    kept.warm()
    assert _eventually(lambda: _KeptFake.made and _KeptFake.made[0].warmed == 1), "warmed when the server starts"
    assert _eventually(lambda: _KeptFake.made[0].warmed >= 2), "and again after an idle stretch"
    # A recall late in a stretch: the next search comes a whole stretch after it, not where the old stretch ended.
    time.sleep(0.7)
    assert kept(_prompt("TEST"), (), (), 5.0)[0] == {"TEST": "TEST"}
    after_recall = _KeptFake.made[0].warmed
    time.sleep(0.6)
    assert _KeptFake.made[0].warmed == after_recall, "a recall starts the idle stretch again"
    assert _eventually(lambda: _KeptFake.made[0].warmed > after_recall), "the stretch after the recall ends in a search"
    kept.close()
    closed = _KeptFake.made[0].warmed
    time.sleep(1.5)
    assert _KeptFake.made[0].warmed == closed and len(_KeptFake.made) == 1, "a closed recaller searches no more"


def test_a_kept_recaller_that_could_not_make_its_handler_makes_none_to_keep_warm(monkeypatch):
    """Keeping warm searches only a handler a recall or the start made: one that could not be made is made by the next
    recall, as before, never in the background."""
    from scope_recall.adapters.clients import local_endpoint

    monkeypatch.setattr(local_endpoint, "KEEP_WARM_IDLE_SECONDS", 0.1)
    monkeypatch.setattr(local_endpoint, "KEEP_WARM_CHECK_SECONDS", 0.02)
    tried = []

    def build():
        tried.append(1)
        raise RuntimeError("TEST no handler")

    kept = local_endpoint.KeptRecaller(build)
    kept.warm()
    assert _eventually(lambda: len(tried) == 1)
    import time

    time.sleep(0.5)
    assert len(tried) == 1, "no handler is made to be kept warm"
    kept.close()


def test_closing_a_recaller_does_not_wait_for_its_keep_warm_search_and_its_handler_is_closed(monkeypatch):
    """Review of 3.5.0rc2: a keep-warm search holds the kept handler as the start's warming does, up to WARM_SECONDS
    (a cold index, a helper to start again).  Closing waited CLOSE_WAIT_SECONDS for it, and when the search outlasted
    that the handler was never closed: the search saw the recaller closed and left it open."""
    import threading
    import time

    from scope_recall.adapters.clients import local_endpoint

    monkeypatch.setattr(local_endpoint, "KEEP_WARM_IDLE_SECONDS", 0.1)
    monkeypatch.setattr(local_endpoint, "KEEP_WARM_CHECK_SECONDS", 0.02)
    monkeypatch.setattr(local_endpoint, "CLOSE_WAIT_SECONDS", 0.5)
    searching, closed = threading.Event(), []

    class Slow(_WarmedHandler):
        searches = 0

        def warm_vectors(self, seconds):
            Slow.searches += 1
            if Slow.searches == 2:  # the first search after the start's
                searching.set()
                time.sleep(1.5)

    kept = local_endpoint.KeptRecaller(lambda: Slow(closed))
    kept.warm()
    assert searching.wait(10)
    started = time.monotonic()
    kept.close()
    waited = time.monotonic() - started
    assert _eventually(lambda: len(closed) == 1), f"the handler was never closed (closing waited {waited:.2f} s)"
    assert waited < 0.3, f"closing waited {waited:.2f} s for the keep-warm search"


def test_a_keep_warm_search_that_failed_is_tried_again_at_once(monkeypatch):
    """Review of 3.5.0rc2: a keep-warm search that found the vector helper gone detaches it and closes the store for
    the next request to open again (vector/process_store.py).  Counted as a use, it left that next request -- a new
    helper, the table and the whole index -- to the next prompt's recall, the cold recall it is there to prevent."""
    import time

    from scope_recall.adapters.clients import local_endpoint

    monkeypatch.setattr(local_endpoint, "KEEP_WARM_IDLE_SECONDS", 1.0)
    monkeypatch.setattr(local_endpoint, "KEEP_WARM_CHECK_SECONDS", 0.05)

    class Gone(_WarmedFake):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.at = []

        def warm_vectors(self, seconds):
            super().warm_vectors(seconds)
            self.at.append(time.monotonic())
            if self.warmed == 2:  # the first keep-warm search after the start's
                raise RuntimeError("TEST native vector worker failed")

    _KeptFake.made = []
    kept = local_endpoint.KeptRecaller(Gone)
    kept.warm()
    try:
        assert _eventually(lambda: _KeptFake.made and len(_KeptFake.made[0].at) >= 3), (
            "the start's, a failed one, another"
        )
        at = _KeptFake.made[0].at
        assert at[2] - at[1] < 0.5, f"searched again {at[2] - at[1]:.2f} s after the failure, not at once"
    finally:
        kept.close()


def test_a_recall_that_did_not_search_by_meaning_does_not_put_off_the_keep_warm_search(monkeypatch):
    """Review of 3.5.0rc2: every recall that took the kept handler counted as a search of its index, also one whose
    query embedding failed (a provider outage, a proxy down) and never reached it.  Prompts all through an outage kept
    the index from being searched, and the first recall after it found the index cold."""
    import time

    from scope_recall.adapters.clients import local_endpoint

    monkeypatch.setattr(local_endpoint, "KEEP_WARM_IDLE_SECONDS", 1.0)
    monkeypatch.setattr(local_endpoint, "KEEP_WARM_CHECK_SECONDS", 0.05)

    class Outage(_WarmedFake):
        def resident_recall_for(self, payload, current_refs, gaps, remaining):
            answer = super().resident_recall_for(payload, current_refs, gaps, remaining)
            self.diagnostics.recall_vectors = False  # the query embedding timed out: no vector search ran
            return answer

    _KeptFake.made = []
    kept = local_endpoint.KeptRecaller(Outage)
    kept.warm()
    try:
        assert _eventually(lambda: _KeptFake.made and _KeptFake.made[0].warmed == 1)
        ends = time.monotonic() + 2.5
        while time.monotonic() < ends:  # a prompt every 0.4 s for 2.5 s, none of whose recalls searched the index
            kept(_prompt("TEST outage"), (), (), 5.0)
            time.sleep(0.4)
        assert _KeptFake.made[0].warmed >= 2, "no search of the index for 2.5 s, with a stretch of 1 s"
    finally:
        kept.close()


def test_a_recall_while_the_kept_handler_is_busy_is_answered_by_its_own():
    """One recall holds the kept handler at a time; another at the same moment gets nothing from it at once and
    recalls as every recall did before, instead of waiting behind the first."""
    import threading

    from scope_recall.adapters.clients.local_endpoint import KeptRecaller

    _KeptFake.made = []
    entered, release = threading.Event(), threading.Event()

    class Slow(_KeptFake):
        def resident_recall_for(self, payload, current_refs, gaps, remaining):
            entered.set()
            assert release.wait(10)
            return super().resident_recall_for(payload, current_refs, gaps, remaining)

    kept = KeptRecaller(Slow)
    first = threading.Thread(target=kept, args=(_prompt("TEST first"), (), (), 5.0))
    first.start()
    assert entered.wait(10)
    try:
        assert kept(_prompt("TEST second"), (), (), 5.0) is None
    finally:
        release.set()
        first.join(10)
    assert kept(_prompt("TEST third"), (), (), 5.0)[0] == {"TEST": "TEST third"} and len(_KeptFake.made) == 1


def test_a_kept_handler_that_raised_or_could_not_attach_its_runtime_is_made_anew():
    """A handler whose runtime is not attached from a config it could read never attaches again: kept, it recalled
    every later prompt without its vector search (review of rc12).  One whose recall raised is not trusted with the
    next."""
    import pytest
    from scope_recall.adapters.clients.local_endpoint import KeptRecaller

    _KeptFake.made = []
    kinds = iter(({"fail": True}, {"attach_failed": True}, {}))
    kept = KeptRecaller(lambda: _KeptFake(**next(kinds)))
    with pytest.raises(RuntimeError):
        kept(_prompt("TEST raised"), (), (), 5.0)
    assert kept(_prompt("TEST no runtime"), (), (), 5.0)[0] == {"TEST": "TEST no runtime"}
    assert kept(_prompt("TEST kept"), (), (), 5.0)[0] == {"TEST": "TEST kept"}
    assert _eventually(lambda: [fake.closed for fake in _KeptFake.made] == [True, True, False])
    assert len(_KeptFake.made) == 3


def test_the_mcp_server_keeps_its_recall_handler_across_prompts_and_threads(resident, monkeypatch):
    """Each prompt's recall reaches the server on a thread of its own; one handler answers them all, and the answer
    is the same as a handler of its own gives."""
    import threading

    from scope_recall.adapters.clients import handler as handler_module

    _root, client, endpoint = resident
    # This store has no runtime config, so no handler's runtime is ready and each would be made anew (the test
    # after this one); the wiring is what is tested here.
    monkeypatch.setattr(handler_module.CodexHookHandler, "runtime_ready", property(lambda self: True))
    made = []
    real = handler_module.CodexHookHandler.from_home.__func__

    def counted(cls, *args, **kwargs):
        made.append(1)
        return real(cls, *args, **kwargs)

    monkeypatch.setattr(handler_module.CodexHookHandler, "from_home", classmethod(counted))
    answers = []

    def ask(index):
        request = {
            "payload": _prompt("TEST 家里的猫叫什么？", prompt_id=f"TEST-prompt-kept-{index}"),
            "current_refs": (),
            "gaps": (),
            "remaining": 5.0,
        }
        answer, close = endpoint.recall(request)
        close()
        answers.append(answer)

    for index in range(3):
        worker = threading.Thread(target=ask, args=(index,))
        worker.start()
        worker.join(30)
    assert len(answers) == 3 and len(made) == 1
    assert all(answer["result"] == answers[0]["result"] for answer in answers)
    endpoint.kept._lock.acquire()
    try:
        answer, close = endpoint.recall(
            {
                "payload": _prompt("TEST 家里的猫叫什么？", prompt_id="TEST-prompt-own"),
                "current_refs": (),
                "gaps": (),
                "remaining": 5.0,
            }
        )
        close()
    finally:
        endpoint.kept._lock.release()
    assert len(made) == 2 and answer["result"] == answers[0]["result"], "a busy kept handler: one of its own"
    endpoint.stop()
    assert endpoint.kept(_prompt("TEST stopped"), (), (), 5.0) is None


def _eventually(check, seconds=5.0):
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_a_server_without_a_runtime_config_makes_a_handler_for_each_recall(resident, monkeypatch):
    """A handler whose runtime is not ready is not kept, so an entry without a runtime config costs each recall what
    it did before rc12, and one whose config could not be read at one moment reads it again at the next."""
    from scope_recall.adapters.clients import handler as handler_module

    _root, _client, endpoint = resident
    made = []
    real = handler_module.CodexHookHandler.from_home.__func__
    monkeypatch.setattr(
        handler_module.CodexHookHandler,
        "from_home",
        classmethod(lambda cls, *args, **kwargs: made.append(1) or real(cls, *args, **kwargs)),
    )
    for index in range(2):
        _answer, close = endpoint.recall(
            {
                "payload": _prompt("TEST 没有配置。", prompt_id=f"TEST-prompt-bare-{index}"),
                "current_refs": (),
                "gaps": (),
                "remaining": 5.0,
            }
        )
        close()
    assert len(made) == 2


def test_a_replaced_kept_handler_is_closed_off_the_request_s_time():
    """Closing a handler stops its vector helper, which can take seconds; done inside the recall that replaced it,
    it spent that recall's time and could make the server look stuck (review of rc12)."""
    import time

    from scope_recall.adapters.clients.local_endpoint import KeptRecaller

    _KeptFake.made = []

    class SlowClose(_KeptFake):
        def close(self):
            time.sleep(1.0)
            super().close()

    stamp = ["one"]
    kept = KeptRecaller(SlowClose, stamp=lambda: stamp[0])
    kept(_prompt("TEST first"), (), (), 5.0)
    stamp[0] = "two"
    started = time.monotonic()
    assert kept(_prompt("TEST second"), (), (), 5.0)[0] == {"TEST": "TEST second"}
    assert time.monotonic() - started < 0.5
    assert _eventually(lambda: _KeptFake.made[0].closed)


def test_a_kept_handler_is_made_anew_when_the_entry_s_pointer_or_grants_change(resident):
    """Before rc12 each prompt read the entry's pointer and the store's record of its grants again; a kept handler
    watches them, so a re-attach that narrows the grants is taken up at the next prompt (review of rc12)."""
    import os

    from scope_recall.adapters.clients.local_endpoint import entry_files

    _root, client, endpoint = resident
    files = entry_files(client)
    assert len(files) == 2 and all(path.is_file() for path in files)
    before = endpoint._kept_stamp()
    for path in files:
        status = path.stat()
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))
        assert endpoint._kept_stamp() != before
        before = endpoint._kept_stamp()


def _embedding_entry(base, monkeypatch, *, delay=0.0):
    """A shared store whose Claude Code entry has a runtime: an embedding route to a loopback server that closes a
    connection idle for 1 s, as a provider closes an idle keep-alive one, and answers each request after ``delay``
    seconds, and a SQLite vector store."""
    import http.server
    import threading
    import time
    from pathlib import Path

    from scope_recall.adapters.hermes.installation import (
        attach_shared_entry,
        build_installation_manifest,
        new_shared_payload,
        read_shared_payload,
        write_shared_payload,
    )
    from scope_recall.maintenance.shared import attach
    from scope_recall.runtime import models
    from scope_recall.runtime.instance import RuntimeInstanceConfig
    from scope_recall.runtime.worker_entry import load_config
    from scope_recall.vector.store import build_vector_store

    vector = [0.125] * 64
    connections = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 1.0

        def do_POST(self):
            connections.append(self.client_address)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(delay)
            body = json.dumps(
                {
                    "object": "list",
                    "model": "TEST-embed",
                    "data": [{"object": "embedding", "index": 0, "embedding": vector}],
                    "usage": {"prompt_tokens": 7, "total_tokens": 7},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, *args):
            return

        def log_error(self, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    helper = Path(models.__file__).resolve().parents[1] / "runtime" / "_http_worker.py"
    worker = base / "TEST-embedding-worker.py"
    worker.write_text(
        "import importlib.util, http.client, time\n"
        f"s = importlib.util.spec_from_file_location('worker', {str(helper)!r})\n"
        "w = importlib.util.module_from_spec(s); s.loader.exec_module(w)\n"
        "def opener(host, port, *, deadline):\n"
        f"    c = http.client.HTTPConnection('127.0.0.1', {server.server_address[1]})\n"
        "    c.timeout = max(0.001, deadline - time.monotonic())\n"
        "    return c\n"
        "w._open_https_connection = opener\n"
        "raise SystemExit(w.main())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_HTTP_WORKER_PATH", worker)
    monkeypatch.setenv("TEST_EMBED_KEY", "TEST-not-a-real-key-0000")

    root = base / "TEST-shared"
    write_shared_payload(root, new_shared_payload(root, agent_id="TEST-agent"))
    home = base / "TEST-tianshu-home"
    home.mkdir()
    attach_shared_entry(
        root,
        build_installation_manifest(
            home, agent_id="TEST-agent", user_id="TEST-owner", agent_workspace="TEST-workspace"
        ),
        entry_id="tianshu",
        display_name="TEST",
        now="2026-09-24T20:00:00Z",
    )
    payload = read_shared_payload(root)
    routes = {
        "binding": {
            "agent_id": payload["agent_id"],
            "installation_id": payload["installation_id"],
            "data_directory": str(root.resolve()),
            "scope_ids": payload["scope_ids"],
            "test_mode": payload["test_mode"],
            "installation_kind": "shared",
        },
        "session_id": "TEST-background",
        "allowed_scope_ids": payload["scope_ids"],
        "owner_id": "TEST-worker",
        "auxiliary": {
            "external_embedding": True,
            "external_consolidation": False,
            "installation_dir": str(root.resolve()),
            "budget": {
                "batch": "TEST-rc12",
                "cap_micro_usd": 100_000_000,
                "total_input_cap": 100_000_000,
                "total_output_cap": 100_000_000,
                "total_call_cap": 100_000,
                "max_request_bytes": 32_000,
                "approved_models": ["TEST-embed"],
                "pricing": {"TEST-embed": {"input_usd_per_million": "0.01", "output_usd_per_million": "0"}},
            },
            "embedding": {
                "credential_env": "TEST_EMBED_KEY",
                "model": "TEST-embed",
                "endpoint": "https://TEST.invalid/v1/embeddings",
                "dimensions": 64,
                "dialect": "openai",
            },
        },
    }
    space = RuntimeInstanceConfig.from_mapping(routes).embedding_space_id()
    routes["vector"] = {
        "backend": "sqlite-bruteforce",
        "storage_dir": str(root.resolve() / "vectors" / space),
        "table_name": "scope_recall",
        "dimensions": 64,
    }
    routes_path = base / "TEST-routes.json"
    routes_path.write_text(json.dumps(routes), encoding="utf-8")
    client = base / "TEST-embedding-claude-code-home"
    client.mkdir()
    attach(
        host="claude-code",
        instance_root=client,
        root=root,
        entry_id="claude-code",
        display_name="Claude Code",
        runtime_config_from=routes_path,
        grants_like=("tianshu",),
        capture_like="tianshu",
        now="2026-09-24T20:00:00Z",
    )
    entry_config = client / "scope-recall" / "runtime-config.json"
    vectors = load_config(entry_config).vector
    store = build_vector_store(
        vectors.backend, storage_dir=vectors.storage_dir, table_name=vectors.table_name, dimensions=vectors.dimensions
    )
    store.open()
    store.close()
    return client, entry_config, server, connections


def test_a_kept_handler_recalls_with_its_vectors_across_a_pause(tmp_path, monkeypatch):
    """A kept handler's embedding worker sent the next prompt's request on the connection its server had closed while
    it sat idle, and the recall went without its vector search (review of rc12).  Asked after pauses longer than the
    server's idle time, each prompt on a thread of its own as a server's are, one handler recalls with its vectors
    every time, on a new connection after each pause."""
    import threading
    import time

    from scope_recall.adapters.clients.handler import CodexHookHandler
    from scope_recall.adapters.clients.local_endpoint import KeptRecaller, entry_files, file_stamp

    client, entry_config, server, connections = _embedding_entry(tmp_path, monkeypatch)
    kept = KeptRecaller(
        lambda: CodexHookHandler.from_home(str(client), "claude-code"),
        stamp=lambda: file_stamp(entry_config, *entry_files(client)),
    )
    outcomes, handlers = [], set()
    try:
        for index, pause in enumerate((0.0, 0.3, 1.8, 1.8)):
            time.sleep(pause)
            box = {}
            prompt = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-kept-session",
                "prompt_id": f"TEST-pause-{index}",
                "prompt": "TEST 家里的猫叫什么名字",
                "cwd": "C:/TEST",
            }
            worker = threading.Thread(target=lambda: box.update(answer=kept(prompt, (), (), 10.0)))
            worker.start()
            worker.join(30)
            diagnostics = box["answer"][1]
            outcomes.append((diagnostics["recall_vectors"], diagnostics["recall_vector_gap"]))
            handlers.add(id(kept._handler))
    finally:
        kept.close()
        server.shutdown()
        server.server_close()
    assert outcomes == [(True, None)] * 4, outcomes
    assert len(handlers) == 1, "one handler throughout"
    assert len(set(connections)) >= 3, "a new connection after each pause"


def _kept_prompt(kept, index, budget=10.0):
    import threading

    box = {}
    prompt = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "TEST-kept-session",
        "prompt_id": f"TEST-slow-{index}",
        "prompt": "TEST 家里的猫叫什么名字",
        "cwd": "C:/TEST",
    }
    worker = threading.Thread(target=lambda: box.update(answer=kept(prompt, (), (), budget)))
    worker.start()
    worker.join(30)
    diagnostics = box["answer"][1]
    return diagnostics["recall_vectors"], diagnostics["recall_vector_gap"]


def test_a_kept_handler_recalls_with_its_vectors_when_its_provider_and_store_are_slow(tmp_path, monkeypatch):
    """The query embedding was asked for after the SQLite channels, with three quarters of what they left: on
    2026-09-29 the work computer's server recalled 4 of 9 prompts by words alone (AuxiliaryModelError:timeout), each
    on a new connection after a pause.  Here the channels take 2.8 s of a prompt's 4 s and the provider answers in
    0.8 s; asked for as the recall starts, the embedding is there when the vector channel needs it, after a pause
    too."""
    import time

    from scope_recall.adapters.clients.handler import CodexHookHandler
    from scope_recall.adapters.clients.local_endpoint import KeptRecaller, entry_files, file_stamp
    from scope_recall.core.retrieval_storage import RetrievalStorage

    client, entry_config, server, connections = _embedding_entry(tmp_path, monkeypatch, delay=0.8)
    lexical = RetrievalStorage.lexical

    def slow_lexical(self, tx, context, *, limit):
        time.sleep(2.8)
        return lexical(self, tx, context, limit=limit)

    monkeypatch.setattr(RetrievalStorage, "lexical", slow_lexical)
    kept = KeptRecaller(
        lambda: CodexHookHandler.from_home(str(client), "claude-code"),
        stamp=lambda: file_stamp(entry_config, *entry_files(client)),
    )
    outcomes = []
    try:
        for index, pause in enumerate((0.0, 1.5)):
            time.sleep(pause)
            outcomes.append(_kept_prompt(kept, index))
    finally:
        kept.close()
        server.shutdown()
        server.server_close()
    assert outcomes == [(True, None)] * 2, outcomes


def test_a_kept_handler_warmed_when_its_server_starts_recalls_its_first_prompt_with_vectors(tmp_path, monkeypatch):
    """Made at the first prompt, a kept handler attached its runtime, opened the table and read the index inside that
    prompt's recall, and the first prompt after every start of its server (every Claude Code session) recalled by
    words alone.  Warmed at the start, the table is open before any prompt; a prompt that comes meanwhile waits for
    the warming instead of making a second handler."""
    import time

    from scope_recall.adapters.clients.handler import CodexHookHandler
    from scope_recall.adapters.clients.local_endpoint import KeptRecaller, entry_files, file_stamp

    client, entry_config, server, connections = _embedding_entry(tmp_path, monkeypatch)
    built = []

    def build():
        built.append(CodexHookHandler.from_home(str(client), "claude-code"))
        return built[-1]

    kept = KeptRecaller(build, stamp=lambda: file_stamp(entry_config, *entry_files(client)))
    waiting = KeptRecaller(build, stamp=lambda: file_stamp(entry_config, *entry_files(client)))
    try:
        kept.warm()
        assert kept._warming.wait(30)
        runtime = kept._handler._host_runtime._runtime
        assert runtime._vector_store is not None, "the table is open before any prompt"
        assert _kept_prompt(kept, 0) == (True, None)
        waiting.warm()
        assert _kept_prompt(waiting, 1) == (True, None), "a prompt during the warming waits for it"
        assert len(built) == 2, "one handler for each recaller"
    finally:
        kept.close()
        waiting.close()
        server.shutdown()
        server.server_close()


class _WarmedHandler:
    runtime_ready = True

    def __init__(self, closed=None, seconds=0.0):
        self._closed, self._seconds = closed, seconds

    def warm_vectors(self, seconds):
        import time

        time.sleep(self._seconds)

    def close(self):
        if self._closed is not None:
            self._closed.append(self)


def test_closing_a_recaller_does_not_wait_for_its_warming():
    """A warming holds the kept handler for up to a minute; closing waited for it, and a server stopped just after
    its start hung that long (review of 3.4.1).  The warming sees the recaller closed and closes its handler."""
    import time

    from scope_recall.adapters.clients.local_endpoint import KeptRecaller

    closed = []
    kept = KeptRecaller(lambda: _WarmedHandler(closed, seconds=1.5))
    kept.warm()
    time.sleep(0.2)
    started = time.monotonic()
    kept.close()
    assert time.monotonic() - started < 1.0, "closing did not wait for the warming"
    assert kept._warming.wait(10) and kept._handler is None
    assert _eventually(lambda: len(closed) == 1), "the warming closed the handler it made"


def test_a_warming_takes_its_stamp_before_its_build():
    """Taken after the build, a change to the entry's files during the build counted as seen, and the handler made
    from the old files was kept (review of 3.4.1)."""
    from scope_recall.adapters.clients.local_endpoint import KeptRecaller

    files = {"stamp": "v1"}

    def build():
        files["stamp"] = "v2"  # the files change while the handler is made
        return _WarmedHandler()

    kept = KeptRecaller(build, stamp=lambda: files["stamp"])
    try:
        kept.warm()
        assert kept._warming.wait(10)
        assert kept._made_with == "v1", "the next recall sees the change and makes the handler anew"
    finally:
        kept.close()


def test_the_mcp_server_warms_its_kept_handler_when_it_starts(store, monkeypatch):
    from scope_recall.adapters.clients import local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    _root, _homes, client, _capture = store
    endpoint = local_endpoint.serve(client, "claude-code")
    assert endpoint is not None
    try:
        assert endpoint.kept._warming is not None and endpoint.kept._warming.wait(30)
    finally:
        endpoint.stop()


# -- WorkBuddy -----------------------------------------------------------------
# WorkBuddy's hooks speak this protocol with three differences: they name no turn their prompt and Stop share, the
# person's words can come wrapped in WorkBuddy's own blocks, and its session record has a layout of its own.

WB_SESSION = "TEST-wb-session"


@pytest.fixture
def workbuddy(store, tmp_path, monkeypatch):
    """A WorkBuddy entry beside the store's others, its session records in a projects folder of the test's own."""
    from scope_recall.adapters.clients import transcript

    root, _homes, _client, _capture = store
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    home = tmp_path / "TEST-workbuddy-home"
    attach_shared_record(
        root,
        client_entry_record(
            host="workbuddy",
            home=home,
            entry_id="workbuddy",
            display_name="WorkBuddy",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    projects = tmp_path / "TEST-workbuddy-projects"
    monkeypatch.setattr(transcript, "workbuddy_projects", lambda: projects)
    return root, home, projects


def _wb(home, payload):
    """One WorkBuddy hook, in a handler of its own as each hook is a process of its own; its answer and diagnostics."""
    hook = CodexHookHandler.from_home(str(home), "workbuddy")
    try:
        result = hook.handle_payload({"session_id": WB_SESSION, "cwd": "C:/TEST/work", **payload})
    finally:
        hook.close()
    return result, hook.diagnostics


def _wb_prompt(text, **fields):
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": text,
        "transcript_path": "C:/TEST/projects/c--TEST-work/TEST-wb-session.jsonl",
        **fields,
    }


def _wb_stop(record=None, last=None, **fields):
    payload = {"hook_event_name": "Stop", "stop_hook_active": False, **fields}
    if record is not None:
        payload["transcript_path"] = str(record)
    if last is not None:
        payload["last_assistant_message"] = last
    return payload


def _wb_ms():
    start = int((datetime.now(timezone.utc) - timedelta(seconds=30)).timestamp() * 1000)
    return lambda seconds: start + seconds * 1000


def _wb_line(role, entry_id, stamp, text, **fields):
    kind = "input_text" if role == "user" else "output_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": kind, "text": text}],
        "id": entry_id,
        "parentId": None,
        "sessionId": WB_SESSION,
        "timestamp": stamp,
        "status": "completed",
        **fields,
    }


def _wb_record(projects, *rows):
    return _record(projects / "c--TEST-work" / f"{WB_SESSION}.jsonl", *rows)


def _wb_said(root, role=None):
    where = f" AND role='{role}'" if role else ""
    return sorted(_rows(root, f"SELECT role, origin, content FROM source_events WHERE entry_id='workbuddy'{where}"))


def _wb_turns(root, kind):
    """The turns of the WorkBuddy session's ``kind`` (user, assistant) sources, in the order they were stored."""
    prefix = f"workbuddy:{read_shared_payload(root)['installation_id']}:{WB_SESSION}:{kind}:"
    keys = _rows(root, "SELECT source_event_key FROM source_events WHERE entry_id='workbuddy' ORDER BY rowid")
    return [key[len(prefix) : -len("@1")] for (key,) in keys if key.startswith(prefix)]


def test_workbuddy_stores_the_person_s_last_query_and_recalls_for_it(workbuddy, store):
    """WorkBuddy hands its prompt hook every user message of the input, joined.  The person's words are the last
    ``<user_query>`` block; the reminders around it are WorkBuddy's, never stored and never searched for."""
    root, home, _projects = workbuddy
    _root, homes, _client, _capture = store
    told = _hermes(homes["tianquan"])
    try:
        told.on_turn_start(1, "TEST 白鹭项目的负责人是 KZ-42。", turn_id="TEST-turn-1", session_id="TEST-session-1")
        told.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。"
        )
        told.sync_turn("TEST 白鹭项目的负责人是 KZ-42。", "好的。", session_id="TEST-session-1")
    finally:
        told.shutdown()
    prompt = (
        "<system-reminder>TEST 当前目录是 C:/TEST/work。</system-reminder>\n"
        "<user_query>TEST 上一条已经答过的问题。</user_query>\n"
        '<system-reminder data-role="tool-hint">TEST 工具提示。</system-reminder>\n'
        "<user_query>白鹭项目的负责人 KZ-42 是谁</user_query>"
    )
    result, diagnostics = _wb(home, _wb_prompt(prompt))
    assert diagnostics.last_reason != "recall_exception", diagnostics.recall_error_detail
    assert _wb_said(root) == [("user", "human_direct", "白鹭项目的负责人 KZ-42 是谁")]
    guidance, _newline, body = result["hookSpecificOutput"]["additionalContext"].partition("\n")
    assert "You are WorkBuddy (workbuddy)" in guidance
    assert any("KZ-42" in item["content"] for item in json.loads(body)["items"])


def test_a_workbuddy_turn_without_an_id_is_opened_by_its_prompt_and_closed_by_its_stop(workbuddy):
    """A prompt with no ``generation_id`` (a session's first) opens a turn of its own, kept for the Stop that closes it;
    each hook is a process of its own.  The session's end removes what was kept."""
    root, home, _projects = workbuddy
    _wb(home, _wb_prompt("TEST 第一个问题。"))
    _wb(home, _wb_stop(last="TEST 第一个回答。"))
    _wb(home, _wb_prompt("TEST 第二个问题。"))
    _wb(home, _wb_stop(last="TEST 第二个回答。"))
    users = _wb_turns(root, "user")
    assert len(set(users)) == 2 and all(turn.startswith("turn-") for turn in users), users
    assert _wb_turns(root, "assistant") == users, "each Stop closes the turn its prompt opened"
    assert list((home / "scope-recall" / "turns").iterdir())
    _wb(home, {"hook_event_name": "SessionEnd", "reason": "clear"})
    assert not list((home / "scope-recall" / "turns").iterdir())


def test_a_workbuddy_generation_id_names_the_turn_its_prompt_opens(workbuddy):
    """``generation_id`` is the session's latest model request: a prompt carries the previous turn's last one and its
    Stop this turn's, so the Stop closes the turn its prompt opened.  A prompt whose id already names a kept turn (one
    stopped before its first request) opens a turn of its own, and both prompts are stored."""
    root, home, _projects = workbuddy
    _wb(home, _wb_prompt("TEST 第一问。"))
    _wb(home, _wb_stop(last="TEST 第一答。", generation_id="TEST-request-1"))
    _wb(home, _wb_prompt("TEST 第二问。", generation_id="TEST-request-1"))
    _wb(home, _wb_stop(last="TEST 第二答。", generation_id="TEST-request-3"))
    _wb(home, _wb_prompt("TEST 第三问，没等回答就停了。", generation_id="TEST-request-3"))
    _wb(home, _wb_prompt("TEST 第四问。", generation_id="TEST-request-3"))
    users = _wb_turns(root, "user")
    assert users[1:3] == ["TEST-request-1", "TEST-request-3"], users
    assert users[0].startswith("turn-") and users[3].startswith("turn-") and users[3] != users[0]
    assert _wb_turns(root, "assistant") == [users[0], "TEST-request-1"]
    assert len(_wb_said(root, "user")) == 4


def test_a_workbuddy_record_its_hook_names_wrongly_is_found_by_the_session(workbuddy):
    """WorkBuddy's ``transcript_path`` has been reported wrong: ``.json`` for ``.jsonl``, or cut two characters short.
    The record is then found by the session's id in WorkBuddy's projects folders."""
    root, home, projects = workbuddy
    at = _wb_ms()
    record = _wb_record(
        projects,
        _wb_line("user", "u1", at(0), "<user_query>TEST 只在记录里的问题。</user_query>"),
        _wb_line("assistant", "a1", at(1), "TEST 只在记录里的回答。"),
    )
    _result, diagnostics = _wb(home, _wb_stop(str(record)[:-1]))
    assert "capture_gap:session_record_unavailable" not in diagnostics.capability_gaps
    _record(record, _wb_line("assistant", "a2", at(2), "TEST 后来的一段。"))
    _wb(home, _wb_stop(str(record)[:-2]))
    assert _wb_said(root) == sorted(
        [
            ("user", "human_direct", "TEST 只在记录里的问题。"),
            ("assistant", "assistant_visible", "TEST 只在记录里的回答。"),
            ("assistant", "assistant_visible", "TEST 后来的一段。"),
        ]
    )
    _result, diagnostics = _wb(home, _wb_stop(str(record)[:-1], session_id="TEST-other-session"))
    assert "capture_gap:session_record_unavailable" in diagnostics.capability_gaps, "another session's is not read"


def test_a_workbuddy_stop_records_what_was_said_between_tool_calls(workbuddy):
    """The Stop hook carries the last reply only; the record has every message.  The person's message there keeps the
    line breaks WorkBuddy takes out of the prompt it hands the hook, and is known by its turn, not stored twice.
    Reasoning, tool calls and results, titles, snapshots and WorkBuddy's own notices are not anyone's words."""
    root, home, projects = workbuddy
    at = _wb_ms()
    record = _wb_record(
        projects,
        _wb_line(
            "user",
            "u1",
            at(0),
            "<system-reminder>TEST 提醒。</system-reminder>\n<user_query>TEST 第一行\nTEST 第二行</user_query>",
        ),
        {
            "type": "reasoning",
            "id": "r1",
            "timestamp": at(1),
            "summary": [{"type": "summary_text", "text": "TEST 想法"}],
        },
        _wb_line("assistant", "a1", at(2), "TEST 我先看一下目录。"),
        {"type": "function_call", "id": "f1", "callId": "c1", "name": "TEST-ls", "arguments": "{}", "timestamp": at(3)},
        {
            "type": "function_call_result",
            "id": "f2",
            "callId": "c1",
            "timestamp": at(4),
            "output": {"type": "text", "text": "TEST 工具输出"},
        },
        _wb_line("assistant", "a2", at(5), "TEST 目录里有三个文件。"),
        _wb_line(
            "user",
            "n1",
            at(6),
            "<task-notification>\n<task-id>TEST</task-id>\n</task-notification>",
            providerData={"isMeta": True},
        ),
        {"type": "ai-title", "id": "t1", "title": "TEST 标题", "timestamp": at(7)},
        {"type": "file-history-snapshot", "id": "s1", "timestamp": at(8)},
    )
    # As WorkBuddy 5.3.14 hands the prompt to its hook: reminders and tags removed, and the newlines with them.
    _wb(home, _wb_prompt("TEST 第一行TEST 第二行"))
    _wb(home, _wb_stop(record, last="TEST 目录里有三个文件。"))
    _wb(home, _wb_stop(record, last="TEST 目录里有三个文件。"))
    assert _wb_said(root) == sorted(
        [
            ("user", "human_direct", "TEST 第一行TEST 第二行"),
            ("assistant", "assistant_visible", "TEST 我先看一下目录。"),
            ("assistant", "assistant_visible", "TEST 目录里有三个文件。"),
        ]
    )


def test_a_workbuddy_turn_whose_stop_never_came_keeps_its_message_from_being_stored_twice(workbuddy):
    """A turn whose Stop never fired (WorkBuddy closed in the middle of it) stays kept.  The next Stop's read of the
    record finds both of the person's messages under the turns their prompts opened, although the record keeps the
    line breaks the hook had without, and stores neither again."""
    root, home, projects = workbuddy
    at = _wb_ms()
    record = _wb_record(
        projects,
        _wb_line("user", "u1", at(0), "<user_query>TEST 第一行\nTEST 第二行</user_query>"),
        _wb_line("user", "u2", at(5), "<user_query>TEST 第三行\nTEST 第四行</user_query>"),
        _wb_line("assistant", "a2", at(6), "TEST 第二个回答。"),
    )
    _wb(home, _wb_prompt("TEST 第一行TEST 第二行"))
    _wb(home, _wb_prompt("TEST 第三行TEST 第四行"))
    _wb(home, _wb_stop(record, last="TEST 第二个回答。"))
    assert _wb_said(root, "user") == [
        ("user", "human_direct", "TEST 第一行TEST 第二行"),
        ("user", "human_direct", "TEST 第三行TEST 第四行"),
    ]


def test_the_entry_s_server_recalls_a_workbuddy_prompt_for_the_person_s_words_under_the_hook_s_turn(
    workbuddy, monkeypatch
):
    """The entry's server answers the prompt hook's recall in a process of its own (``resident_recall_for``).  It takes
    the person's words out of the prompt as the hook does, and the turn the hook kept for them, so the two recall the
    same text under the same request."""
    _root, home, _projects = workbuddy
    prompt = _wb_prompt(
        "<system-reminder>TEST 提醒。</system-reminder>\n<user_query>TEST 服务这边的问题。</user_query>",
        generation_id="TEST-request-9",
    )
    _wb(home, prompt)
    asked = []
    monkeypatch.setattr(
        "scope_recall.adapters.clients.prompt_recall.PromptRecall.own",
        lambda self, context, text, request_id, *rest: asked.append((text, request_id)) or {},
    )
    server = CodexHookHandler.from_home(str(home), "workbuddy")
    try:
        server.resident_recall_for({"session_id": WB_SESSION, "cwd": "C:/TEST/work", **prompt}, (), (), 5.0)
    finally:
        server.close()
    assert asked == [("TEST 服务这边的问题。", f"workbuddy-auto:{WB_SESSION}:TEST-request-9")]


def test_a_workbuddy_agent_run_and_task_notice_are_not_the_person_s(workbuddy):
    """A subagent's hooks (its record id ``agent-*``) are an agent speaking, and a background task's notice is
    WorkBuddy's: neither is stored as the person's or recalled for.  ``agent_type`` alone names the agent that runs the
    person's own session, which WorkBuddy sets on every turn after the first: those turns stay the person's."""
    root, home, _projects = workbuddy
    run = {"agent_id": "agent-TEST1", "agent_type": "TEST-explorer"}
    result, diagnostics = _wb(home, _wb_prompt("TEST 子代理收到的任务。", **run))
    assert result == {} and diagnostics.last_reason == "agent_run"
    _result, diagnostics = _wb(home, _wb_stop(last="TEST 子代理的结论。", **run))
    assert diagnostics.last_reason == "agent_run"
    notice = "<task-notification><task-id>TEST</task-id><status>completed</status></task-notification>"
    result, diagnostics = _wb(home, _wb_prompt(notice))
    assert result == {} and diagnostics.last_reason == "task_notification"
    # A Stop hook's or a goal's request that the turn go on, as WorkBuddy hands it to the hook (newlines removed).
    result, diagnostics = _wb(home, _wb_prompt("Stop hook feedback:[TEST 目标]: TEST 还没完成，继续。"))
    assert result == {} and diagnostics.last_reason == "task_notification"
    _wb(home, _wb_prompt("TEST 一句真话。", agent_type="craft"))
    _wb(home, _wb_stop(last="TEST 好的。", agent_type="craft"))
    assert _wb_said(root) == sorted(
        [("user", "human_direct", "TEST 一句真话。"), ("assistant", "assistant_visible", "TEST 好的。")]
    )


def test_a_workbuddy_stop_that_repeats_the_last_reply_stores_nothing(workbuddy):
    """A turn that failed or was stopped before it said anything hands the Stop the reply of the turn before."""
    root, home, _projects = workbuddy
    _wb(home, _wb_prompt("TEST 第一问。"))
    _wb(home, _wb_stop(last="TEST 第一答。"))
    _wb(home, _wb_prompt("TEST 第二问，马上停了。"))
    _wb(home, _wb_stop(last="TEST 第一答。"))
    assert _wb_said(root, "assistant") == [("assistant", "assistant_visible", "TEST 第一答。")]
    _wb(home, _wb_prompt("TEST 第三问。"))
    _wb(home, _wb_stop(last="TEST 第三答。"))
    assert len(_wb_said(root, "assistant")) == 2, "a new reply is stored"


WB_NOTICE = "Authentication required. Please use /login command to sign in to your account"
WB_ERROR = {"error": {"message": WB_NOTICE, "isNetworkError": False, "isStreamTimeout": False, "isRetryable": False}}


def _wb_answered_then_failed(home, projects, at):
    """A first turn answered (its reply stored), then a turn whose model could not answer: WorkBuddy shows the notice,
    records it with its error, and hands it to the Stop."""
    record = _wb_record(projects, _wb_line("user", "u1", at(0), "<user_query>TEST 第一问。</user_query>"))
    _wb(home, _wb_prompt("TEST 第一问。"))
    _record(record, _wb_line("assistant", "a1", at(1), "TEST 第一答。"))
    _wb(home, _wb_stop(record, last="TEST 第一答。"))
    _record(record, _wb_line("user", "u2", at(3), "<user_query>TEST 第二问。</user_query>"))
    _wb(home, _wb_prompt("TEST 第二问。"))
    _record(record, _wb_line("assistant", "a2", at(4), WB_NOTICE, status="incomplete", providerData=WB_ERROR))
    _result, diagnostics = _wb(home, _wb_stop(record, last=WB_NOTICE))
    assert diagnostics.last_reason == "client_error_reply"
    return record


def test_a_workbuddy_error_shown_in_place_of_a_reply_is_not_stored(workbuddy):
    """WorkBuddy hands the Stop the error it showed when the model could not answer (here not signed in) as the reply;
    its record marks that message with the error.  The person's prompt is kept, the notice is not, from the Stop or
    from the record (whose read still moves past it), and the same question sent again after signing in is answered
    and stored as usual."""
    from scope_recall.adapters.clients import transcript

    root, home, projects = workbuddy
    at = _wb_ms()
    question = "TEST 天姬今天出了什么事？"
    record = _wb_record(projects, _wb_line("user", "u1", at(0), f"<user_query>{question}</user_query>"))
    _wb(home, _wb_prompt(question))
    _record(record, _wb_line("assistant", "a1", at(1), WB_NOTICE, status="incomplete", providerData=WB_ERROR))
    _result, diagnostics = _wb(home, _wb_stop(record, last=WB_NOTICE))
    assert diagnostics.last_reason == "client_error_reply"
    assert _wb_said(root) == [("user", "human_direct", question)]
    assert transcript.Cursor(home, WB_SESSION, record).load() == record.stat().st_size, "the read moved past it"
    _record(record, _wb_line("user", "u2", at(3), f"<user_query>{question}</user_query>"))
    _wb(home, _wb_prompt(question))
    _record(record, _wb_line("assistant", "a2", at(4), "TEST 网关停了六分钟。"))
    _wb(home, _wb_stop(record, last="TEST 网关停了六分钟。"))
    assert _wb_said(root, "assistant") == [("assistant", "assistant_visible", "TEST 网关停了六分钟。")]
    assert _wb_said(root, "user") == [("user", "human_direct", question)] * 2


def test_a_workbuddy_turn_stopped_after_an_error_hands_either_the_error_or_the_reply_before_it(workbuddy):
    """A turn stopped before it said anything hands its Stop the reply before it.  After an error turn that may be the
    error or the last real reply: neither is stored again, and the error's words are kept until a new reply comes."""
    root, home, projects = workbuddy
    at = _wb_ms()
    record = _wb_answered_then_failed(home, projects, at)
    _record(record, _wb_line("user", "u3", at(6), "<user_query>TEST 停一下。</user_query>"))
    _wb(home, _wb_prompt("TEST 停一下。"))
    _wb(home, _wb_stop(record, last=WB_NOTICE))
    _record(record, _wb_line("user", "u4", at(8), "<user_query>TEST 又停了。</user_query>"))
    _wb(home, _wb_prompt("TEST 又停了。"))
    _result, diagnostics = _wb(home, _wb_stop(record, last="TEST 第一答。"))
    assert diagnostics.last_reason == "repeated_reply"
    assert _wb_said(root, "assistant") == [("assistant", "assistant_visible", "TEST 第一答。")]


def test_a_workbuddy_turn_that_said_something_and_hands_the_error_stores_only_what_it_said(workbuddy):
    """A turn that wrote a message and was then stopped, its Stop handed the error of the turn before (the record's last
    model message, the new one, carries no error): the error is the kept one and is not stored; the record read
    stores what the turn did say."""
    root, home, projects = workbuddy
    at = _wb_ms()
    record = _wb_answered_then_failed(home, projects, at)
    _record(record, _wb_line("user", "u3", at(6), "<user_query>TEST 再查一次。</user_query>"))
    _wb(home, _wb_prompt("TEST 再查一次。"))
    _record(record, _wb_line("assistant", "a3", at(7), "TEST 我先看一下日志。", status="incomplete"))
    _wb(home, _wb_stop(record, last=WB_NOTICE))
    stored = sorted(content for _role, _origin, content in _wb_said(root, "assistant"))
    assert stored == sorted(["TEST 第一答。", "TEST 我先看一下日志。"])


def test_a_workbuddy_record_that_cannot_be_looked_up_still_keeps_the_stop_s_reply(workbuddy, monkeypatch):
    """The error check runs before the capture.  A record lookup that fails (a folder it may not list) says no there,
    and the Stop stores its reply as before; the record read meets the same failure after it."""
    from scope_recall.adapters.clients import transcript

    root, home, _projects = workbuddy
    _wb(home, _wb_prompt("TEST 问。"))

    def refused(*_args, **_kwargs):
        raise PermissionError(13, "TEST access denied")

    monkeypatch.setattr(transcript, "workbuddy_record_path", refused)
    with pytest.raises(PermissionError):
        _wb(home, _wb_stop(last="TEST 一个真的回答。"))
    assert _wb_said(root, "assistant") == [("assistant", "assistant_visible", "TEST 一个真的回答。")]


def test_a_workbuddy_reply_cut_by_an_error_keeps_what_was_shown(workbuddy):
    """A reply that broke off (a stream timeout) carries the error too, but its words are the model's: the Stop stores
    them under its turn, and the record read recognises them."""
    root, home, projects = workbuddy
    at = _wb_ms()
    record = _wb_record(projects, _wb_line("user", "u1", at(0), "<user_query>TEST 写一段长说明。</user_query>"))
    _wb(home, _wb_prompt("TEST 写一段长说明。"))
    _record(
        record,
        _wb_line(
            "assistant",
            "a1",
            at(1),
            "TEST 第一部分写到这里",
            status="incomplete",
            providerData={
                "error": {"message": "TEST stream timed out", "isNetworkError": False, "isStreamTimeout": True}
            },
        ),
    )
    _result, diagnostics = _wb(home, _wb_stop(record, last="TEST 第一部分写到这里"))
    assert diagnostics.last_reason != "client_error_reply"
    assert _wb_said(root, "assistant") == [("assistant", "assistant_visible", "TEST 第一部分写到这里")]
    assert len(_wb_turns(root, "assistant")) == 1, "stored by the Stop, under its turn"


def test_a_workbuddy_reply_said_again_in_a_new_turn_is_stored_from_the_record(workbuddy):
    """The Stop skips a reply that repeats the session's last one, which is what a turn stopped before it said anything
    hands it; when the record shows the turn did say those words again, after the person's message, they are stored
    from there."""
    root, home, projects = workbuddy
    at = _wb_ms()
    record = _wb_record(projects, _wb_line("user", "u1", at(0), "<user_query>TEST 把第一个文件改名。</user_query>"))
    _wb(home, _wb_prompt("TEST 把第一个文件改名。"))
    _record(record, _wb_line("assistant", "a1", at(2), "TEST 好的。"))
    _wb(home, _wb_stop(record, last="TEST 好的。", generation_id="TEST-request-1"))
    _record(record, _wb_line("user", "u2", at(4), "<user_query>TEST 停一下。</user_query>"))
    _wb(home, _wb_prompt("TEST 停一下。", generation_id="TEST-request-1"))
    _wb(home, _wb_stop(record, last="TEST 好的。", generation_id="TEST-request-1"))
    assert _wb_said(root, "assistant") == [("assistant", "assistant_visible", "TEST 好的。")], "a stopped turn"
    _record(record, _wb_line("user", "u3", at(6), "<user_query>TEST 再把第二个文件改名。</user_query>"))
    _wb(home, _wb_prompt("TEST 再把第二个文件改名。", generation_id="TEST-request-1"))
    _record(record, _wb_line("assistant", "a3", at(8), "TEST 好的。"))
    _wb(home, _wb_stop(record, last="TEST 好的。", generation_id="TEST-request-2"))
    assert _wb_said(root, "assistant") == [("assistant", "assistant_visible", "TEST 好的。")] * 2


def test_a_workbuddy_record_s_own_user_messages_are_not_the_person_s(workbuddy):
    """WorkBuddy saves what the person sent inside ``<user_query>``.  The user messages it adds itself carry none: a
    local command and its output, a shell command run in bash mode and its output, a teammate's report, and a slash
    command's expansion, which WorkBuddy writes over the typed command before it saves the message (the prompt hook is
    handed the typed command)."""
    root, home, projects = workbuddy
    at = _wb_ms()
    skill = (
        "<command-message>review</command-message> <command-name>/review</command-name> "
        "<command-args>a.py</command-args>\nBase directory for this skill: C:/TEST/skills/review\n"
        "TEST the skill's own instructions: list every defect and propose a fix."
    )
    record = _wb_record(
        projects,
        _wb_line("user", "b1", at(0), "<bash-input>dir</bash-input>", providerData={"skipRun": True}),
        _wb_line(
            "user",
            "b2",
            at(1),
            "<bash-stdout>TEST a.py\nTEST b.py</bash-stdout><bash-stderr></bash-stderr>",
            providerData={"skipRun": True},
        ),
        _wb_line(
            "user",
            "c1",
            at(2),
            "<command-name>/model</command-name><command-args>TEST</command-args>",
            providerData={"skipRun": True},
        ),
        _wb_line(
            "user",
            "c2",
            at(3),
            "<local-command-stdout>TEST switched</local-command-stdout>",
            providerData={"skipRun": True},
        ),
        _wb_line(
            "user",
            "t1",
            at(4),
            '<teammate-message teammate_id="TEST" summary="TEST">\nTEST done\n</teammate-message>',
            providerData={"teammateMessage": {"from": "TEST"}},
        ),
        _wb_line("user", "u1", at(5), skill),
        _wb_line("assistant", "a1", at(6), "TEST a.py 没有问题。"),
    )
    _wb(home, _wb_prompt("/review a.py"))
    _wb(home, _wb_stop(record, last="TEST a.py 没有问题。"))
    assert _wb_said(root, "user") == [("user", "human_direct", "/review a.py")]


def test_a_workbuddy_message_queued_while_a_turn_ran_is_kept_from_the_record(workbuddy):
    """Messages sent while a turn ran are merged into one, a ``<user_query>`` block each, and the prompt hook is handed
    only the last: the others are read from the record."""
    root, home, projects = workbuddy
    at = _wb_ms()
    first, second = "TEST 第一件事：把表格导出。", "TEST 第二件事：查一下 QX-17。"
    merged = _wb_line(
        "user", "u1", at(0), f"<system-reminder>TEST 提醒</system-reminder>\n<user_query>{first}</user_query>"
    )
    merged["content"].append({"type": "input_text", "text": f"<user_query>{second}</user_query>"})
    record = _wb_record(projects, merged, _wb_line("assistant", "a1", at(1), "TEST 都办好了。"))
    _wb(home, _wb_prompt(second))
    _wb(home, _wb_stop(record, last="TEST 都办好了。"))
    said = [content for _role, _origin, content in _wb_said(root, "user")]
    assert any(first in content for content in said) and any(second in content for content in said), said


def test_a_workbuddy_prompt_runs_the_entry_s_budget(workbuddy):
    _root, home, _projects = workbuddy
    (home / "scope-recall" / "runtime-config.json").write_text(
        json.dumps({"hook_processing_seconds": 5.5}), encoding="utf-8"
    )
    hook = CodexHookHandler.from_home(str(home), "workbuddy")
    try:
        assert hook._hook_budget() == 5.5
    finally:
        hook.close()


def test_a_workbuddy_prompt_hook_answers_within_its_budget(workbuddy, small_reserve, monkeypatch, capsys):
    """A WorkBuddy prompt hook that runs past its wait blocks the prompt.  With the entry's server slower than the
    hook's whole budget, the hook still answers inside the budget, with the prompt stored."""
    import io
    import time

    from scope_recall.adapters.clients import hook_entry, local_endpoint
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    root, home, _projects = workbuddy
    endpoint = local_endpoint.serve(home, "workbuddy", warm=False)
    assert endpoint is not None
    try:
        calls = _counted(endpoint, monkeypatch, delay=3.0)
        raw = json.dumps(
            {
                "session_id": WB_SESSION,
                "cwd": "C:/TEST/work",
                **_wb_prompt("<user_query>TEST 服务答得太慢。</user_query>"),
            }
        ).encode()
        monkeypatch.setattr(hook_entry.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(raw)})())
        started = time.monotonic()
        assert hook_entry.main(["--home", str(home), "--host", "workbuddy"]) == 0
        elapsed = time.monotonic() - started
        captured = capsys.readouterr()
        # No runtime config names a budget here: the hook's own 2 s (``_TOTAL_BUDGET_S``).
        assert elapsed < 2.0 + 1.0, f"the hook took {elapsed:.1f} s of its 2 s"
        assert not captured.out or "additionalContext" in json.loads(captured.out)["hookSpecificOutput"]
        assert "CODEX_RECALL_RESIDENT:late" in captured.err
        assert len(calls) == 1
        assert _wb_said(root, "user") == [("user", "human_direct", "TEST 服务答得太慢。")]
        time.sleep(3.0)  # the server's late recall ends; it writes nothing
    finally:
        endpoint.stop()
    assert _wb_said(root, "user") == [("user", "human_direct", "TEST 服务答得太慢。")]


def test_a_workbuddy_hook_with_nothing_to_add_writes_nothing(workbuddy, tmp_path, monkeypatch, capsys):
    """WorkBuddy puts a prompt hook's whole stdout in front of the prompt unless it carries additionalContext, so the
    "{}" the other clients read as nothing would have stood before every prompt that recalled nothing."""
    import io

    from scope_recall.adapters.clients import hook_entry
    from scope_recall.vector import process_store

    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: None)
    _root, home, _projects = workbuddy

    def answer(payload, where, host):
        raw = json.dumps({"session_id": WB_SESSION, "cwd": "C:/TEST/work", **payload}).encode()
        monkeypatch.setattr(hook_entry.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(raw)})())
        assert hook_entry.main(["--home", str(where), "--host", host]) == 0
        return capsys.readouterr().out

    assert answer(_wb_stop(last="TEST 答。"), home, "workbuddy") == "", "a Stop never has anything to add"
    unattached = tmp_path / "TEST-not-attached"
    assert answer(_wb_prompt("TEST 问。"), unattached, "workbuddy") == ""
    assert answer(_wb_prompt("TEST 问。"), unattached, "claude-code") == "{}"


# -- DeepSeek Harness (dsh): a plugin runs these hooks (``distribution/dsh``) ---------------------------------------

DSH_SESSION = "session-TEST-dsh"


@pytest.fixture
def dsh(store, tmp_path):
    """A dsh entry beside the store's others, the owner at this machine."""
    root, _homes, _client, _capture = store
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    home = tmp_path / "TEST-dsh-home"
    attach_shared_record(
        root,
        client_entry_record(
            host="dsh",
            home=home,
            entry_id="dsh",
            display_name="DeepSeek Harness",
            attached_at=NOW,
            allowed_scope_ids=owner["allowed_scope_ids"],
            writable_scope_ids=owner["writable_scope_ids"],
            capture_scope_id=owner["capture_scope_id"],
        ),
        now=NOW,
    )
    return root, home


def _dsh(home, payload, *, session=DSH_SESSION):
    """One hook as dsh's plugin runs it, in a handler of its own as each is a process of its own."""
    hook = CodexHookHandler.from_home(str(home), "dsh")
    try:
        result = hook.handle_payload({"session_id": session, "cwd": "C:/TEST/work", **payload})
    finally:
        hook.close()
    return result, hook.diagnostics


def _dsh_ms():
    start = int((datetime.now(timezone.utc) - timedelta(seconds=30)).timestamp() * 1000)
    return lambda seconds: start + seconds * 1000


def _dsh_said(root, role=None):
    where = f" AND role='{role}'" if role else ""
    return sorted(_rows(root, f"SELECT role, origin, content FROM source_events WHERE entry_id='dsh'{where}"))


def test_a_dsh_prompt_is_stored_under_its_turn_and_another_session_recalls_it(dsh):
    """The plugin runs the prompt hook before a turn's first step: the prompt is the owner's, named by the session and
    dsh's turn number, and the answer is what is remembered, which the plugin appends to the step."""
    root, home = dsh
    result, diagnostics = _dsh(
        home, {"hook_event_name": "UserPromptSubmit", "turn_id": "1", "prompt": "TEST 我的猫叫 Mochi，最爱吃金枪鱼。"}
    )
    assert diagnostics.last_event == "UserPromptSubmit"
    assert _dsh_said(root) == [("user", "human_direct", "TEST 我的猫叫 Mochi，最爱吃金枪鱼。")]
    keys = [key for (key,) in _rows(root, "SELECT source_event_key FROM source_events WHERE entry_id='dsh'")]
    assert keys == [f"dsh:{read_shared_payload(root)['installation_id']}:{DSH_SESSION}:user:1@1"]
    result, _diagnostics = _dsh(
        home,
        {"hook_event_name": "UserPromptSubmit", "turn_id": "1", "prompt": "TEST 我的猫叫什么？"},
        session="session-TEST-dsh-2",
    )
    context = (result.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "Mochi" in context, "a new session's first prompt recalls what another one said"


def test_a_dsh_stop_stores_the_turn_s_messages_once_and_says_how_many(dsh):
    """The plugin keeps a turn's messages and sends them with its Stop: the reply under the turn, and from the lines what
    the model said while it worked and what the person sent meanwhile.  The answer's ``through`` is how many lines are
    stored; the same lines sent again (an answer lost on the way) store nothing twice."""
    root, home = dsh
    at = _dsh_ms()
    _dsh(home, {"hook_event_name": "UserPromptSubmit", "turn_id": "3", "prompt": "TEST 把两个文件改名。"})
    record = [
        {"id": "u1", "role": "user", "text": "TEST 把两个文件改名。", "time": at(0)},
        {"id": "a1", "role": "assistant", "text": "TEST 我先看一下目录。", "time": at(1)},
        {"id": "u2", "role": "user", "text": "TEST 顺便把第三个也改了。", "time": at(2)},
        {"id": "a2", "role": "assistant", "text": "TEST 三个文件都改好了。", "time": at(3)},
    ]
    stop = {
        "hook_event_name": "Stop",
        "turn_id": "3",
        "last_assistant_message": "TEST 三个文件都改好了。",
        "record": record,
    }
    result, _diagnostics = _dsh(home, stop)
    assert result == {"through": 4}
    expected = sorted(
        [
            ("user", "human_direct", "TEST 把两个文件改名。"),
            ("assistant", "assistant_visible", "TEST 我先看一下目录。"),
            ("user", "human_direct", "TEST 顺便把第三个也改了。"),
            ("assistant", "assistant_visible", "TEST 三个文件都改好了。"),
        ]
    )
    assert _dsh_said(root) == expected
    result, _diagnostics = _dsh(home, stop)
    assert result == {"through": 4} and _dsh_said(root) == expected, "sent again, nothing is stored twice"


def test_a_dsh_turn_without_a_reply_stores_what_its_lines_show(dsh):
    """A turn that failed or was aborted has no reply, and the plugin sends what it kept of earlier turns the same way:
    no reply is stored, the lines are, and lines that are no message are counted with them."""
    root, home = dsh
    at = _dsh_ms()
    record = [
        {"id": "u1", "role": "user", "text": "TEST 这一轮没有回答。", "time": at(0)},
        {"id": "", "role": "user", "text": "TEST 没有 id", "time": at(1)},
        {"id": "x1", "role": "system", "text": "TEST 不是人说的", "time": at(1)},
    ]
    result, diagnostics = _dsh(home, {"hook_event_name": "Stop", "record": record})
    assert diagnostics.last_reason != "missing_turn_id"
    assert result == {"through": 3}
    assert _dsh_said(root) == [("user", "human_direct", "TEST 这一轮没有回答。")]


def test_dsh_sends_no_session_end(dsh):
    _root, home = dsh
    result, diagnostics = _dsh(home, {"hook_event_name": "SessionEnd", "reason": "other"})
    assert result == {} and diagnostics.last_reason == "unsupported_event"


# -- dsh's plugin (``distribution/dsh/scope-recall/index.mjs``) driven as dsh drives it, without dsh -------------------
# node runs the plugin with a fake ``ctx`` (``dsh_harness/harness.mjs``) through one turn; the plugin runs the real hook
# client against the test store.  The harness drops the interpreter's ``-I`` (``dsh_harness/hooks.mjs``), so the hook
# imports this checkout through tests/sitecustomize.py.  Skipped where node (20.6 or later, for module hooks) is not
# installed; the gate lets the host tier run the node it found (``SCOPE_RECALL_TEST_NODE``).

DSH_PLUGIN = Path(__file__).resolve().parents[3] / "distribution" / "dsh" / "scope-recall" / "index.mjs"
DSH_HARNESS = Path(__file__).resolve().parent / "dsh_harness"
NODE = os.environ.get("SCOPE_RECALL_TEST_NODE") or __import__("shutil").which("node")


def _node_ok() -> bool:
    if NODE is None:
        return False
    try:
        version = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        major, minor = (int(part) for part in version.lstrip("v").split(".")[:2])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return (major, minor) >= (20, 6)


_NEEDS_NODE = pytest.mark.skipif(not _node_ok(), reason="node 20.6 or later is not installed")


def _run_plugin(config: dict, scenario: str = "turn", env: dict | None = None) -> dict:
    process = subprocess.run(
        [
            NODE,
            "--import",
            (DSH_HARNESS / "register.mjs").as_uri(),
            str(DSH_HARNESS / "harness.mjs"),
            str(DSH_PLUGIN),
            json.dumps(config),
            scenario,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={**os.environ, **(env or {})},
    )
    assert process.returncode == 0, process.stderr[-2000:]
    return json.loads(process.stdout.strip().splitlines()[-1])


def _plugin_rows(root):
    return sorted(
        _rows(
            root,
            "SELECT role, origin, content FROM source_events WHERE entry_id='dsh' "
            "AND source_event_key LIKE '%session-TEST-plugin%'",
        )
    )


def _plugin_keys(root):
    """What each of the plugin session's rows was stored as: ``user:1`` (the prompt hook), ``assistant:1`` (a Stop's
    reply) or ``record:<id>`` (a line of the turn's messages)."""
    return sorted(
        key.split(":session-TEST-plugin:", 1)[1].rsplit("@", 1)[0]
        for (key,) in _rows(
            root,
            "SELECT source_event_key FROM source_events WHERE entry_id='dsh' AND source_event_key LIKE "
            "'%session-TEST-plugin%'",
        )
    )


@_NEEDS_NODE
def test_the_plugin_recalls_before_the_first_step_and_stores_the_turn_at_its_end(dsh, tmp_path):
    root, home = dsh
    _dsh(
        home,
        {"hook_event_name": "UserPromptSubmit", "turn_id": "1", "prompt": "TEST 我的猫叫 Mochi，最爱吃金枪鱼。"},
        session="session-TEST-seed",
    )
    spool = tmp_path / "TEST-spool"
    result = _run_plugin(
        {
            "python": sys.executable,
            "home": str(home),
            "spool": str(spool),
            "version": "TEST",
            "prompt": "TEST 我的猫叫什么名字？",
            "queued": "TEST 先说一句：我在家。",
        }
    )
    assert result["warnings"] == []
    assert result["decisionKept"], "the step's own messages and flags are passed on"
    assert result["injected"] is not None and "Mochi" in result["injected"]["text"]
    assert (result["injected"]["kind"], result["injected"]["form"]) == ("plugin:scope-recall", "recall")
    assert result["spool"] == [], "the turn was stored and nothing is left on disk"
    assert result["status"]["lastRecall"]["outcome"] == "recalled"
    assert result["status"]["lastStore"]["error"] is None and result["status"]["backlog"] == 0
    assert _plugin_rows(root) == sorted(
        [
            ("user", "human_direct", "TEST 先说一句：我在家。"),
            ("user", "human_direct", "TEST 我的猫叫什么名字？"),
            ("assistant", "assistant_visible", "TEST 我先查一下记忆。"),
            ("assistant", "assistant_visible", "TEST 它叫 Mochi。"),
        ]
    ), (
        "the prompt (the step's last message of the person's) and the reply once each, the message taken with the "
        "prompt and what was said in between from the record, and none of dsh's own context"
    )
    assert _plugin_keys(root) == ["assistant:1", "record:a-1", "record:u-1", "user:1"], "the reply is the turn's"


@_NEEDS_NODE
def test_a_turn_that_did_not_complete_has_no_reply_and_keeps_what_was_said(dsh, tmp_path):
    """A turn the person stopped (or that failed) after the model said something has no reply: what was said is stored
    from the turn's messages, once, and nothing is stored as the turn's reply."""
    root, home = dsh
    result = _run_plugin(
        {"python": sys.executable, "home": str(home), "spool": str(tmp_path / "TEST-spool"), "endReason": "aborted"}
    )
    assert result["spool"] == [] and result["status"]["lastStore"]["error"] is None
    assert _plugin_keys(root) == ["record:a-1", "record:a-2", "user:1"]


@_NEEDS_NODE
def test_a_message_that_comes_while_a_turn_is_stored_is_kept_and_stored_with_the_next(dsh, tmp_path):
    """The person's next message, and the next turn, come while the first turn's Stop runs: the store rewrites the spool
    from what it holds then, not from what it read before, and stores the rest when that turn ends."""
    root, home = dsh
    result = _run_plugin(
        {"python": sys.executable, "home": str(home), "spool": str(tmp_path / "TEST-spool"), "overlap": True}
    )
    assert result["spool"] == [] and result["warnings"] == []
    rows = _plugin_rows(root)
    assert ("user", "human_direct", "TEST 第二轮的问题。") in rows
    assert ("assistant", "assistant_visible", "TEST 第二轮的回答。") in rows
    assert len(rows) == len(set(rows)) == 5


@_NEEDS_NODE
def test_a_turn_larger_than_one_stop_is_stored_in_several_within_the_hook_s_input(dsh, tmp_path):
    """Two model messages of 20,000 CJK characters each (60 KB of UTF-8 apiece): each is clipped to what one Stop can
    carry, they go in Stops of their own under the hook's 64 KiB of input, and the reply, too large to go beside its
    line, is stored from it."""
    root, home = dsh
    result = _run_plugin(
        {
            "python": sys.executable,
            "home": str(home),
            "spool": str(tmp_path / "TEST-spool"),
            "bigText": {"char": "长", "count": 20_000},
        }
    )
    assert result["spool"] == [] and result["status"]["lastStore"]["error"] is None, result["warnings"]
    assert _plugin_keys(root) == ["record:a-1", "record:a-2", "user:1"]
    stored = [content for role, _origin, content in _plugin_rows(root) if role == "assistant"]
    assert all(
        content.endswith("more characters not kept by Scope Recall]") and len(content.encode("utf-8")) < 36_000
        for content in stored
    )


@_NEEDS_NODE
def test_a_stop_that_stores_part_of_its_lines_is_followed_by_the_next_without_waiting(dsh, tmp_path):
    """A Stop stores what fits in its time; the next one takes the rest at once, so a backlog is not left to the sweep's
    waits (the stand-in hook stores one line each time)."""
    _root, home = dsh
    log = tmp_path / "TEST-hook-log.jsonl"
    result = _run_plugin(
        {"python": sys.executable, "home": str(home), "spool": str(tmp_path / "TEST-spool")},
        env={"SR_FAKE_HOOK": str(DSH_HARNESS / "fake_hook.mjs"), "SR_FAKE_HOOK_LOG": str(log)},
    )
    stops = [
        len(payload["record"])
        for payload in map(json.loads, log.read_text(encoding="utf-8").splitlines())
        if payload["hook_event_name"] == "Stop"
    ]
    assert stops == [3, 2, 1], "each Stop sends what is left"
    assert result["spool"] == [] and result["status"]["lastStore"]["error"] is None
    assert result["status"]["retryAfter"] is None


@_NEEDS_NODE
def test_a_hook_that_fails_says_why_in_the_status_and_keeps_the_turn(dsh, tmp_path):
    """An interpreter that cannot import the package (a venv moved, say) exits 1: the recall's outcome and the store's
    error say so with the end of its stderr, instead of reading as nothing recalled."""
    _root, home = dsh
    result = _run_plugin(
        {"python": sys.executable, "home": str(home), "spool": str(tmp_path / "TEST-spool"), "expectBacklog": True},
        env={"SR_FAKE_HOOK": str(DSH_HARNESS / "fake_hook.mjs"), "SR_FAKE_HOOK_MODE": "broken"},
    )
    assert result["status"]["lastRecall"]["outcome"].startswith("exit 1: ")
    assert "No module named 'scope_recall'" in result["status"]["lastRecall"]["outcome"]
    assert "exit 1: " in result["status"]["lastStore"]["error"] and result["status"]["backlog"] == 3


@_NEEDS_NODE
def test_a_hook_that_cannot_run_keeps_the_turn_on_disk_and_the_step_goes_on(dsh, tmp_path):
    _root, home = dsh
    spool = tmp_path / "TEST-spool"
    result = _run_plugin(
        {"python": str(tmp_path / "TEST-no-python.exe"), "home": str(home), "spool": str(spool), "expectBacklog": True}
    )
    assert result["injected"] is None and result["decisionKept"], "no recall, and the turn is not failed"
    assert sorted(result["spool"]) == ["assistant", "assistant", "turn_end", "user"], "kept to store later"
    assert result["status"]["lastStore"]["error"] and result["status"]["backlog"] == 3
    assert any("kept to store later" in warning for warning in result["warnings"])


@_NEEDS_NODE
def test_an_aborted_step_is_passed_on_untouched(dsh, tmp_path):
    _root, home = dsh
    spool = tmp_path / "TEST-spool"
    result = _run_plugin({"python": sys.executable, "home": str(home), "spool": str(spool)}, "aborted")
    assert result["decisionKept"] and result["warnings"] == [] and result["spool"] == []


@_NEEDS_NODE
def test_the_plugin_stores_what_a_dsh_that_is_gone_left_and_leaves_a_running_one_s_file(dsh, tmp_path):
    """A dsh that ended before its store did leaves its spool file; another dsh's plugin takes it, once it is idle and its
    process is gone, and stores it.  That turn ended minutes ago, so its reply is stored from the record alone, once (sent
    as the reply too, it would be stored again: the store compares moments 120 s apart at most).  A message older than
    14 days is dropped and said; the file of a process that still runs is left to it."""
    root, home = dsh
    spool = tmp_path / "TEST-spool"
    spool.mkdir()
    gone = int(
        subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True
        ).stdout
    )
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    minute = 60_000
    session = "session-TEST-plugin"

    def line(key, role, text, at, *, of=session, turn=4):
        return json.dumps(
            {
                "k": key,
                "sessionId": of,
                "role": role,
                "id": f"id-{key}",
                "text": text,
                "time": at,
                "turn": turn,
                "cwd": "C:/TEST/work",
            },
            ensure_ascii=False,
        )

    left = spool / f"{session}.{gone}.jsonl"
    left.write_text(
        "\n".join(
            [
                line("old", "user", "TEST 半个月前的话。", now - 15 * 24 * 60 * minute, turn=1),
                line("u4", "user", "TEST 第四轮的问题。", now - 10 * minute),
                line("a4", "assistant", "TEST 第四轮的回答。", now - 10 * minute + 5_000),
                json.dumps(
                    {
                        "k": "e4",
                        "sessionId": session,
                        "role": "turn_end",
                        "turn": 4,
                        "reason": "completed",
                        "time": now - 10 * minute + 6_000,
                        "cwd": "C:/TEST/work",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    running = spool / f"session-TEST-other.{os.getpid()}.jsonl"
    running.write_text(
        line("x", "user", "TEST 还在跑的那个 dsh 的话。", now - 10 * minute, of="session-TEST-other") + "\n",
        encoding="utf-8",
    )
    idle = datetime.now(timezone.utc).timestamp() - 600
    for path in (left, running):
        os.utime(path, (idle, idle))
    result = _run_plugin(
        {"python": sys.executable, "home": str(home), "spool": str(spool), "ignore": [running.name]}, "sweep"
    )
    assert result["files"] == [running.name] and result["spool"] == ["user"], "a running process's file is its own"
    assert _plugin_rows(root) == sorted(
        [("user", "human_direct", "TEST 第四轮的问题。"), ("assistant", "assistant_visible", "TEST 第四轮的回答。")]
    )
    assert result["status"]["dropped"] == 1 and result["status"]["backlog"] == 1
    assert [warning for warning in result["warnings"] if "dropped unstored" in warning], result["warnings"]


@_NEEDS_NODE
def test_a_sweep_that_fails_stops_at_the_first_session_and_waits_longer(dsh, tmp_path):
    """Two dsh processes that are gone left a file each.  The store cannot be reached (here: no interpreter), so the pass
    stops at the first session it tried, keeps that one's messages, leaves the other file as it was and says when it
    tries again: a backlog after an outage is not sent to the store all at once, nor every minute."""
    _root, home = dsh
    spool = tmp_path / "TEST-spool"
    spool.mkdir()
    gone = int(
        subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True
        ).stdout
    )
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    idle = datetime.now(timezone.utc).timestamp() - 600
    names = []
    for index in (1, 2):
        name = f"session-TEST-gone-{index}.{gone}.jsonl"
        (spool / name).write_text(
            json.dumps(
                {
                    "k": f"k{index}",
                    "sessionId": f"session-TEST-gone-{index}",
                    "role": "user",
                    "id": f"u{index}",
                    "text": f"TEST 第 {index} 个会话。",
                    "time": now - 600_000,
                    "turn": 1,
                    "cwd": "C:/TEST/work",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(spool / name, (idle, idle))
        names.append(name)
    result = _run_plugin(
        {
            "python": str(tmp_path / "TEST-no-python.exe"),
            "home": str(home),
            "spool": str(spool),
            "expectBacklog": True,
            "waitMs": 30_000,
            "waitStatus": "retryAfter",
        },
        "sweep",
    )
    untouched = [name for name in names if name in result["files"]]
    taken = [name for name in result["files"] if name not in names]
    assert len(untouched) == 1 and len(taken) == 1, result["files"]
    assert taken[0].endswith(f".{result['pid']}.jsonl"), "the first is taken into this process's file and kept"
    assert taken[0].split(".")[0] != untouched[0].split(".")[0]
    assert (
        result["status"]["retryAfter"] and result["status"]["lastStore"]["error"] and result["status"]["backlog"] == 2
    )
