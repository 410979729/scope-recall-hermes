"""A local client attached to a shared store: Claude Code, through the Codex adapter.

Two Hermes homes and a Claude Code home share one store.  What the owner types
into Claude Code is the owner's, recorded under the client's entry, and a Hermes
entry recalls it; what a Hermes entry was told reaches the client's prompt.
Sources are synthetic; nothing here is a person's memory.
"""
from __future__ import annotations

from contextlib import closing
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


def test_claude_code_s_prompt_runs_the_entry_s_budget_and_codex_keeps_its_two_seconds(store, tmp_path):
    """Recall on the pilot's shared store took 2.7-5.7 s.  Claude Code waits 15 s for a prompt's hook,
    Codex 2 s, and the runtime that carries the configured budget is attached only after the capture."""
    _root, _homes, client, _capture = store
    (client / "scope-recall" / "runtime-config.json").write_text(json.dumps({"hook_processing_seconds": 5.5}),
                                                                 encoding="utf-8")
    hook = _hook(client)
    try:
        assert hook._hook_budget() == 5.5
    finally:
        hook.close()
    (client / "scope-recall" / "runtime-config.json").write_text(json.dumps({"hook_processing_seconds": 60}),
                                                                 encoding="utf-8")
    hook = _hook(client)
    try:
        assert hook._hook_budget() == 2.0, "an out-of-bounds budget falls back to the default"
    finally:
        hook.close()


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
