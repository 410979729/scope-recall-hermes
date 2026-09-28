"""A local client attached to a shared store: Claude Code, through the Codex adapter.

Two Hermes homes and a Claude Code home share one store.  What the owner types
into Claude Code is the owner's, recorded under the client's entry, and a Hermes
entry recalls it; what a Hermes entry was told reaches the client's prompt.
Sources are synthetic; nothing here is a person's memory.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from scope_recall.adapters.codex import CodexHookHandler
from scope_recall.adapters.codex.config import CodexConfigError, load_shared_client
from scope_recall.adapters.codex.mcp_server import build_server
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
        attach_shared_entry(root, build_installation_manifest(home, agent_id=AGENT, user_id=OWNER,
                                                              agent_workspace=WORKSPACE),
                            entry_id=name, display_name=display, now=NOW)
        homes[name] = home
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    client = tmp_path / "TEST-claude-code-home"
    attach_shared_record(root, client_entry_record(
        host="claude-code", home=client, entry_id="claude-code", display_name="Claude Code", attached_at=NOW,
        allowed_scope_ids=owner["allowed_scope_ids"], writable_scope_ids=owner["writable_scope_ids"],
        capture_scope_id=owner["capture_scope_id"]), now=NOW)
    return root, homes, client, owner["capture_scope_id"]


def _prompt(text, *, session="TEST-cc-session", prompt_id="TEST-prompt-1", cwd="C:/anywhere/at/all"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt_id": prompt_id, "prompt": text,
            "cwd": cwd, "transcript_path": "C:/TEST/transcript.jsonl", "permission_mode": "default"}


def _hook(client):
    # The system clock, as the Hermes entries use: a fixed earlier "now" would hide their sources as future ones.
    return CodexHookHandler.from_home(str(client), "claude-code")


def _rows(root, sql):
    with closing(sqlite3.connect(root / "memory.sqlite3")) as connection:
        return connection.execute(sql).fetchall()


def _hermes(home):
    provider = ScopeRecallHermesAdapter()
    provider.initialize("TEST-session-1", hermes_home=str(home), platform="cli", agent_context="primary",
                        agent_identity=AGENT, agent_workspace=WORKSPACE, user_id=OWNER, parent_session_id="")
    return provider


def test_a_prompt_is_the_owner_s_under_the_client_s_entry_and_a_hermes_entry_recalls_it(store):
    root, homes, client, capture = store
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 青鸟计划的代号是 QX-17。"))
        assert hook.diagnostics.capture_stage == "source_committed"
        hook.handle_payload({"hook_event_name": "Stop", "session_id": "TEST-cc-session", "prompt_id": "TEST-prompt-1",
                             "last_assistant_message": "好的，记下了。", "cwd": "C:/elsewhere"})
    finally:
        hook.close()

    rows = _rows(root, "SELECT entry_id, session_id, scope_id, role, origin, source_event_key, extra_json "
                       "FROM source_events WHERE entry_id='claude-code' ORDER BY role DESC")
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
    assert any("QX-17" in item["content"] and item["entries"] == [{"id": "claude-code", "name": "Claude Code"}]
               for item in items)


def test_what_a_hermes_entry_was_told_reaches_the_client_s_prompt_marked_as_theirs(store):
    root, homes, client, _capture = store
    told = _hermes(homes["tianquan"])
    try:
        told.on_turn_start(1, "TEST 白鹭项目的负责人是 KZ-42。", turn_id="TEST-turn-1", session_id="TEST-session-1")
        told.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。")
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
        hook.handle_payload({"hook_event_name": "PostToolUse", "session_id": "TEST-cc-session", "prompt_id": "P",
                             "tool_name": "Bash", "tool_use_id": "T1", "tool_input": {"command": "ls"},
                             "tool_response": "TEST output", "cwd": "C:/x"})
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
        assert hook.handle_payload({"hook_event_name": "SessionStart", "session_id": "TEST-cc-session",
                                    "cwd": "C:/anywhere"}) == {}
    finally:
        hook.close()


def _codex_client(root, tmp_path):
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    home = tmp_path / "TEST-codex-home"
    attach_shared_record(root, client_entry_record(
        host="codex", home=home, entry_id="codex", display_name="Codex", attached_at=NOW,
        allowed_scope_ids=owner["allowed_scope_ids"], writable_scope_ids=owner["writable_scope_ids"],
        capture_scope_id=owner["capture_scope_id"]), now=NOW)
    return home


def test_a_client_s_prompt_runs_the_entry_s_budget(store, tmp_path):
    """Recall on the pilot's shared store took 2.7-5.7 s.  Claude Code waits 15 s for a prompt's hook and Codex
    now as long, so both run the entry's configured budget; with 2 s most of Codex's automatic recalls came back
    empty.  No installer writes ``hook_processing_seconds``: a config without it runs the worker's 6 s."""
    root, _homes, client, _capture = store
    codex = _codex_client(root, tmp_path)
    for home, host in ((client, "claude-code"), (codex, "codex")):
        (home / "scope-recall" / "runtime-config.json").write_text(json.dumps({"hook_processing_seconds": 5.5}),
                                                                   encoding="utf-8")
        hook = CodexHookHandler.from_home(str(home), host)
        try:
            assert hook._hook_budget() == 5.5, host
        finally:
            hook.close()
    (client / "scope-recall" / "runtime-config.json").write_text(json.dumps({"auto_recall_seconds": 5.0}),
                                                                 encoding="utf-8")
    hook = _hook(client)
    try:
        assert hook._hook_budget() == 6.0, "a config that does not name the budget runs the worker's default"
    finally:
        hook.close()
    (client / "scope-recall" / "runtime-config.json").write_text(json.dumps({"hook_processing_seconds": 60}),
                                                                 encoding="utf-8")
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
    (codex / "scope-recall" / "runtime-config.json").write_text(json.dumps({"auto_recall_seconds": 5.0}),
                                                                encoding="utf-8")
    hook = CodexHookHandler.from_home(str(codex), "codex")
    try:
        assert hook._hook_budget() == 6.0
        started = datetime.now(timezone.utc)
        hook.handle_payload({"hook_event_name": "UserPromptSubmit", "session_id": "TEST-codex-session",
                             "turn_id": "TEST-turn-1", "prompt": "TEST 周五之前把 QX-17 的报价发出去。",
                             "cwd": "C:/anywhere"})
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
    authorize = build_ingress_authorizer(InstanceBinding(worker["agent_id"], worker["installation_id"], root.resolve(),
                                                         frozenset(worker["scope_ids"]), worker["test_mode"], "shared"))
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
    (copied / "scope-recall" / "attachment.json").write_bytes((client / "scope-recall" / "attachment.json").read_bytes())
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
    return _line("user", uuid, stamp, origin={"kind": "human"}, promptId=prompt_id,
                 message={"role": "user", "content": text})


def _model(uuid, stamp, *blocks):
    return _line("assistant", uuid, stamp, message={"role": "assistant", "model": "TEST-model", "content": list(blocks)})


def _said(text):
    return {"type": "text", "text": text}


def _record(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _stop(record, last=None):
    payload = {"hook_event_name": "Stop", "session_id": "TEST-cc-session", "prompt_id": "TEST-prompt-1",
               "transcript_path": str(record), "cwd": "C:/x", "stop_hook_active": False}
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
        _line("user", "t1", at(4), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "T1", "content": "TEST tool output"}]}),
        _line("attachment", "q1", at(5), attachment={
            "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"}, "prompt": "TEST 顺便看看 KZ-42。"}),
        _line("attachment", "n1", at(6), attachment={
            "type": "queued_command", "commandMode": "task-notification", "prompt": "<task-notification>TEST</task-notification>"}),
        _line("user", "n2", at(7), origin={"kind": "task-notification"},
              message={"role": "user", "content": "<task-notification>TEST</task-notification>"}),
        _line("user", "s1", at(8), isCompactSummary=True, message={"role": "user", "content": "TEST summary of earlier work"}),
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
    assert _said_in_store(root) == sorted([
        ("user", "human_direct", "TEST 帮我查一下 QX-17 的进度。"),
        ("assistant", "assistant_visible", "TEST 我先看一下记录。"),
        ("user", "human_direct", "TEST 顺便看看 KZ-42。"),
        ("assistant", "assistant_visible", "TEST QX-17 已经完成。"),
    ])


def test_later_identical_human_message_with_a_different_prompt_id_is_preserved(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
                     _person("u1", at(0), "TEST 好"))
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 好"))
        hook.handle_payload(_stop(record))
        _record(record, _person("u2", at(1), "TEST 好", prompt_id="TEST-prompt-2"))
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    store_id = read_shared_payload(root)["installation_id"]
    assert sorted(_rows(root, "SELECT source_event_key FROM source_events WHERE role='user' AND entry_id='claude-code'")) == sorted([
        (f"claude-code:{store_id}:TEST-cc-session:user:TEST-prompt-1@1",),
        (f"claude-code:{store_id}:TEST-cc-session:record:u2@1",),
    ])


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
    assert [content for _role, _origin, content in _said_in_store(root)] == ["TEST normal"]


def test_record_check_carries_stop_budget_and_defers_large_schema_upgrade(store, tmp_path, monkeypatch):
    root, _homes, client, capture_scope = store
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
                     _person("u1", _moments()(0), "TEST budgeted record"))
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
        context = TrustedContext(hook.core.config.binding, "claude-code:TEST-cc-session",
                                 hook.config.scope_ids, "host_generated")
        with pytest.raises(ContractError, match="upgrade_pending"):
            original(context, capture_scope, [("user", "TEST different", _moments()(1), None)], remaining_seconds=budgets[0])
        with sqlite3.connect(root / "memory.sqlite3") as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1108
    finally:
        hook.close()


def test_two_record_messages_repeating_hook_text_are_not_both_suppressed(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    text = "TEST 好。"
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
                     _person("u1", at(0), text), _person("u2", at(1), text, prompt_id="TEST-prompt-2"))
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
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
                     _person("u1", at(0), "TEST 帮我查一下 QX-17 的进度。"),
                     _line("attachment", "q1", at(1), attachment={
                         "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"},
                         "prompt": "TEST 顺便看看 KZ-42。"}))
    hook = _hook(client)
    try:
        hook.handle_payload(_prompt("TEST 帮我查一下 QX-17 的进度。"))
        hook.handle_payload(_prompt("TEST 顺便看看 KZ-42。", prompt_id="TEST-prompt-queued"))
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert [content for role, _origin, content in _said_in_store(root) if role == "user"] == sorted(
        ["TEST 帮我查一下 QX-17 的进度。", "TEST 顺便看看 KZ-42。"])


def test_a_prompt_still_in_the_inbox_is_not_stored_again_from_the_record(store, tmp_path, monkeypatch):
    root, _homes, client, _capture = store
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
                     _person("u1", _moments()(0), "TEST 帮我查一下 QX-17 的进度。"))
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
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
                     _person("u1", at(0), "TEST 第一句。"),
                     _model("a1", at(1), _said("TEST 第一段。")),
                     _model("a2", at(2), _said("TEST 第二段。")))
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
    assert [content for _role, _origin, content in _said_in_store(root)] == ["TEST 第一句。"], \
        "the read stops at the message that could not be written"

    _record(record, _model("a3", at(3), _said("TEST 第三段。")))
    hook = _hook(client)
    try:
        hook.handle_payload(_stop(record))
    finally:
        hook.close()
    assert sorted(content for _role, _origin, content in _said_in_store(root)) == sorted(
        ["TEST 第一句。", "TEST 第一段。", "TEST 第二段。", "TEST 第三段。"])


def test_a_lost_or_stale_read_position_costs_a_reread_and_never_a_duplicate(store, tmp_path):
    root, _homes, client, _capture = store
    at = _moments()
    record = _record(tmp_path / "TEST-projects" / "TEST-cc-session.jsonl",
                     _person("u1", at(0), "TEST 一。"), _model("a1", at(1), _said("TEST 二。")))
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
    assert sorted(content for _role, _origin, content in _said_in_store(root)) == sorted(["TEST 一。", "TEST 二。", "TEST 三。"])


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
    attach_shared_record(root, client_entry_record(
        host="codex", home=codex, entry_id="codex", display_name="Codex", attached_at=NOW,
        allowed_scope_ids=owner["allowed_scope_ids"], writable_scope_ids=owner["writable_scope_ids"],
        capture_scope_id=owner["capture_scope_id"]), now=NOW)
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


SUGGESTIONS_PROMPT = ("# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with "
                      "Codex in this local project: C:\\TEST\n\nGet an understanding of the user's intent and goals "
                      "by deeply viewing their connected apps.\n\n# Rules\n\n"
                      + "- TEST rule about what a suggestion must be and must not be.\n" * 120
                      + "\n# Examples\n\n## Bad examples\n\n" + "- TEST bad example.\n" * 60
                      + "\n# Response format\n\nJSON.")


def test_codex_s_request_for_suggestions_is_told_from_the_owner_s_words():
    """It opens with a Markdown heading, names its hyperpersonalized suggestions early and runs to thousands of
    characters; a wording change around that still counts, the owner's own words about it do not."""
    from scope_recall.adapters.codex.boundary import is_codex_suggestions_prompt

    assert len(SUGGESTIONS_PROMPT) >= 8000 and is_codex_suggestions_prompt(SUGGESTIONS_PROMPT)
    reworded = SUGGESTIONS_PROMPT.replace("# Overview\n\nGenerate 0 to 3", "## Overview\n\nPropose up to three")
    assert is_codex_suggestions_prompt(reworded.replace("hyperpersonalized", "Hyperpersonalised"))
    assert not is_codex_suggestions_prompt("帮我看看 Codex 的 hyperpersonalized suggestions 是怎么生成的")
    assert not is_codex_suggestions_prompt("# 我的笔记\n\n" + "今天记下 hyperpersonalized suggestions 这个词。" * 5)
    assert not is_codex_suggestions_prompt("Codex 生成的建议如下。\n" + "hyperpersonalized suggestions\n" * 200)
    # The owner's own long notes about the feature, with a heading and even one of its section names, stay theirs.
    note = ("# 关于 Codex 的 hyperpersonalized suggestions\n\n## Overview\n\n"
            + "我在研究它每次发来的那段提示词，想弄清它为什么被当成我说的话存下来。\n" * 300)
    assert len(note) >= 8000 and not is_codex_suggestions_prompt(note)


def test_codex_s_request_for_suggestions_is_neither_stored_nor_recalled(store, tmp_path):
    """Codex sends its request for suggestions of what to do next through the prompt hook.  On the pilot four were
    stored as the owner's words, 11,000 to 15,000 characters each, claims were drawn from them as if the owner had
    said them, and nine of the model's JSON answers were stored as replies."""
    root, _homes, _client, _capture = store
    codex = _codex_client(root, tmp_path)
    hook = CodexHookHandler.from_home(str(codex), "codex")
    try:
        assert hook.handle_payload({"hook_event_name": "UserPromptSubmit", "session_id": "TEST-codex-session",
                                    "turn_id": "TEST-turn-1", "prompt": SUGGESTIONS_PROMPT, "cwd": "C:/TEST"}) == {}
        assert hook.diagnostics.last_reason == "host_generated_prompt"
        assert hook.handle_payload({"hook_event_name": "Stop", "session_id": "TEST-codex-session",
                                    "turn_id": "TEST-turn-1", "cwd": "C:/TEST",
                                    "last_assistant_message": '{"suggestions":[{"title":"TEST 建议"}]}'}) == {}
        assert hook.diagnostics.last_reason == "host_generated_reply"
        hook.handle_payload({"hook_event_name": "UserPromptSubmit", "session_id": "TEST-codex-session",
                             "turn_id": "TEST-turn-2", "prompt": "TEST 一句真话。", "cwd": "C:/TEST"})
        hook.handle_payload({"hook_event_name": "Stop", "session_id": "TEST-codex-session", "turn_id": "TEST-turn-2",
                             "cwd": "C:/TEST", "last_assistant_message": "TEST 好的。"})
    finally:
        hook.close()
    stored = _rows(root, "SELECT content FROM source_events WHERE entry_id='codex' AND role IN ('user', 'assistant') "
                         "ORDER BY rowid")
    assert stored == [("TEST 一句真话。",), ("TEST 好的。",)]


def test_a_prompt_longer_than_a_recall_query_is_still_recalled_for(store):
    """A recall request carries at most 8,192 characters of query.  A longer prompt went whole, the request was
    refused, and the turn had no recall at all: three of the work computer's Codex prompts in one morning."""
    _root, homes, client, _capture = store
    told = _hermes(homes["tianquan"])
    try:
        told.on_turn_start(1, "TEST 白鹭项目的负责人是 KZ-42。", turn_id="TEST-turn-1", session_id="TEST-session-1")
        told.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。")
        told.sync_turn("TEST 白鹭项目的负责人是 KZ-42。", "好的。", session_id="TEST-session-1")
    finally:
        told.shutdown()
    # Distinct characters, so the prompt has far more than 128 distinct terms as a real one would.
    prompt = ("白鹭项目的负责人 KZ-42 是谁？下面是附件：\n" + "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈剑号巨阙珠称夜光果珍李柰菜重芥姜海咸河淡鳞潜羽翔龙师火帝鸟官人皇始制文字乃服衣裳推位让国有虞陶唐吊民伐罪周发殷汤坐朝问道垂拱平章爱育黎首臣伏戎羌遐迩一体率宾归王鸣凤在竹白驹食场化被草木赖及万方" * 3 + "\n"
              + "TEST 附件里的一行字。\n" * 800)
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


def test_a_failed_recall_says_what_stopped_it(store, monkeypatch, capsys):
    """The work computer's server logged recall_exception three times with nothing else: the cause had to be found
    by reading the store."""
    from scope_recall.adapters.codex.handler import emit_result
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
        store, monkeypatch):
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
        told.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。")
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
    from scope_recall.adapters.codex.handler import recall_without_vectors

    assert recall_without_vectors(["vector_old_or_mismatched_space", "vector_rejected:space"]) is None
    assert recall_without_vectors(["sqlite_candidate_error:OperationalError"]) is None
    assert recall_without_vectors(["vector_unavailable", "vector_error:TimeoutError:helper_open_deadline"]) \
        == "vector_error:TimeoutError:helper_open_deadline"
    assert recall_without_vectors(["deadline_exceeded_collect"]) == "deadline_exceeded_collect"
    assert recall_without_vectors(["sqlite_unavailable:INPUT_INVALID"]) == "sqlite_unavailable:INPUT_INVALID"


def test_a_prompt_hook_starts_the_vector_helper_before_it_stores_the_prompt(monkeypatch):
    """Each hook is a new process, and a vector helper started when the recall reached its vector search spent the
    rest of the recall's budget importing LanceDB: Claude Code and Codex recalled from words alone."""
    from scope_recall.adapters.codex import hook_entry
    from scope_recall.vector import process_store

    started = []
    monkeypatch.setattr(process_store, "prestart", lambda **kwargs: started.append(kwargs))
    monkeypatch.setattr(hook_entry.sys, "platform", "win32")
    hook_entry._prestart_vector_helper(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "TEST"}).encode())
    hook_entry._prestart_vector_helper(json.dumps({"hook_event_name": "Stop",
                                                   "last_assistant_message": "UserPromptSubmit"}).encode())
    hook_entry._prestart_vector_helper(b"not json")
    assert started == [{}]
