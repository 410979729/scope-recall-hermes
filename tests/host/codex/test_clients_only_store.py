"""Clients alone share one store: no Hermes home is attached to it.

``init-shared`` makes the store, WorkBuddy attached first brings the owner's scope into it, and dsh attached naming
WorkBuddy reads and captures there: what the owner tells either client, the other recalls.  A Hermes home attached
later without ``--grants-from`` holds that scope as its own owner scope.  Sources are synthetic; nothing here is a
person's memory.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from scope_recall.adapters.clients import CodexHookHandler, transcript
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter
from scope_recall.adapters.hermes.shared_entries import read_shared_payload
from scope_recall.maintenance.shared import main

AGENT = "TEST-agent"
NAMES = {"workbuddy": "WorkBuddy", "dsh": "DeepSeek Harness", "desk": "Desk"}


def _run(capsys, *argv):
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def made(tmp_path, capsys, monkeypatch):
    """A store ``init-shared`` just made, nothing attached to it; WorkBuddy's session records in the test's folder."""
    monkeypatch.setattr(transcript, "workbuddy_projects", lambda: tmp_path / "TEST-workbuddy-projects")
    root = (tmp_path / "TEST-shared").resolve()
    code, result = _run(capsys, "init-shared", "--root", str(root), "--agent-id", AGENT)
    assert (code, result["status"]) == (0, "initialized"), result
    return root


def _attach(capsys, root, home, host, entry, *grants):
    home.mkdir(exist_ok=True)
    argv = ["attach", "--host", host, "--instance-root", str(home), "--root", str(root)]
    return _run(capsys, *argv, "--entry", entry, "--display-name", NAMES[entry], *grants)


def _clients(tmp_path, capsys, root):
    """WorkBuddy attached first, with no entry to take grants like, and dsh beside it naming WorkBuddy."""
    homes = {host: (tmp_path / f"TEST-{host}-home").resolve() for host in ("workbuddy", "dsh")}
    code, first = _attach(capsys, root, homes["workbuddy"], "workbuddy", "workbuddy")
    assert (code, first.get("new_scopes")) == (0, 1), first
    code, later = _attach(
        capsys, root, homes["dsh"], "dsh", "dsh", "--grants-like", "workbuddy", "--capture-like", "workbuddy"
    )
    assert (code, later.get("new_scopes")) == (0, 0), later
    return homes


def _prompt(home, host, text, *, session, turn="1"):
    """One prompt hook, in a handler of its own as each hook is a process of its own: what it put before the prompt."""
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": "C:/TEST/work", "prompt": text}
    if host == "dsh":
        payload["turn_id"] = turn
    hook = CodexHookHandler.from_home(str(home), host)
    try:
        answer = hook.handle_payload(payload)
    finally:
        hook.close()
    reason = hook.diagnostics.last_reason
    assert reason not in ("recall_exception", "deadline_exceeded"), (reason, hook.diagnostics.recall_error_detail)
    context = (answer.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    return json.loads(context.partition("\n")[2])["items"] if context else []


def _came_through(items, word):
    """The entries each recalled item holding ``word`` came in through."""
    return [[entry["id"] for entry in item["entries"]] for item in items if word in item["content"]]


def _stored(root, entry_id):
    with closing(sqlite3.connect(root / "memory.sqlite3")) as connection:
        return connection.execute(
            "SELECT scope_id, role, origin FROM source_events WHERE entry_id=? ORDER BY rowid", (entry_id,)
        ).fetchall()


def test_two_clients_share_a_store_no_hermes_home_is_attached_to(tmp_path, capsys, made):
    """WorkBuddy, attached first to the new store, brings the owner's scope; dsh, attached naming it, reads and
    captures there.  Each recalls what the owner told the other, marked with the entry it came in through."""
    root = made
    homes = _clients(tmp_path, capsys, root)
    workbuddy, dsh = (load_shared_client(homes[host], host) for host in ("workbuddy", "dsh"))
    scope = workbuddy.audience.capture_scope_id
    assert read_shared_payload(root)["scope_ids"] == [scope]
    assert dsh.audience.capture_scope_id == scope and dsh.audience.allowed_scope_ids == frozenset({scope})

    _prompt(homes["workbuddy"], "workbuddy", "TEST 青鸟计划的代号是 QX-17。", session="TEST-wb-session")
    assert _stored(root, "workbuddy") == [(scope, "user", "human_direct")]
    recalled = _prompt(homes["dsh"], "dsh", "青鸟计划的代号 QX-17 是什么", session="TEST-dsh-session")
    assert ["workbuddy"] in _came_through(recalled, "QX-17")

    _prompt(homes["dsh"], "dsh", "TEST 白鹭项目的负责人是 KZ-42。", session="TEST-dsh-session", turn="2")
    recalled = _prompt(homes["workbuddy"], "workbuddy", "白鹭项目的负责人 KZ-42 是谁", session="TEST-wb-session-2")
    assert ["dsh"] in _came_through(recalled, "KZ-42")


def test_a_hermes_home_attached_later_reads_and_is_read_in_the_clients_scope(tmp_path, capsys, made):
    """A Hermes home attached without ``--grants-from`` gets a fresh installation's owner grants, and the store's first
    client brought exactly that owner scope: the Hermes entry brings no scope of its own, and the owner's words reach
    every agent both ways, with no grants changed."""
    root = made
    homes = _clients(tmp_path, capsys, root)
    desk = (tmp_path / "TEST-desk-home").resolve()
    code, attached = _attach(capsys, root, desk, "hermes", "desk")
    assert (code, attached.get("new_scopes")) == (0, 0), attached

    _prompt(homes["workbuddy"], "workbuddy", "TEST 青鸟计划的代号是 QX-17。", session="TEST-wb-session")
    told = ScopeRecallHermesAdapter()
    told.initialize(
        "TEST-session-1",
        hermes_home=str(desk),
        platform="cli",
        agent_context="primary",
        agent_identity=AGENT,
        agent_workspace="hermes",
        user_id="local",
        parent_session_id="",
    )
    try:
        injected = told.prefetch("青鸟计划的代号 QX-17 是什么")
        told.on_turn_start(1, "TEST 白鹭项目的负责人是 KZ-42。", turn_id="TEST-turn-1", session_id="TEST-session-1")
        told.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 白鹭项目的负责人是 KZ-42。"
        )
        told.sync_turn("TEST 白鹭项目的负责人是 KZ-42。", "好的。", session_id="TEST-session-1")
    finally:
        told.shutdown()
    assert ["workbuddy"] in _came_through(json.loads(injected.partition("\n")[2])["items"], "QX-17")
    recalled = _prompt(homes["dsh"], "dsh", "白鹭项目的负责人 KZ-42 是谁", session="TEST-dsh-session")
    assert ["desk"] in _came_through(recalled, "KZ-42")
