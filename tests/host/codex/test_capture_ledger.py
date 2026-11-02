"""Synthetic ledger recovery after incomplete hook executions, with privacy and deduplication."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

import pytest
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.handler import CodexHookHandler
from scope_recall.adapters.clients.ledger import reconcile_store
from scope_recall.adapters.shared_entries import attach_shared_record, client_entry_record
from scope_recall.contracts import ContractError

from tests.host.codex.test_shared_client import NOW, store


@pytest.fixture(params=["codex", "claude-code", "workbuddy"])
def ledger_client(store, tmp_path, request):
    root, _homes, _client, scope = store
    host = request.param
    home = tmp_path / f"TEST-{host}-ledger"
    attach_shared_record(
        root,
        client_entry_record(
            host=host,
            home=home,
            entry_id="ledger-client",
            display_name="Synthetic ledger",
            attached_at=NOW,
            allowed_scope_ids=[scope],
            writable_scope_ids=[scope],
            capture_scope_id=scope,
        ),
        now=NOW,
    )
    session = "TEST-ledger-session"
    if host == "codex":
        record = home / "sessions/2026/10/01" / f"rollout-TEST-{session}.jsonl"
        rows = [dict(type="session_meta", payload=dict(id=session, cwd=str(tmp_path)), timestamp=NOW)]
        for role, text in [
            ("user", "TEST public <private>hidden segment</private> retained"),
            ("assistant", "TEST reply"),
        ]:
            rows.append(
                dict(
                    timestamp=NOW,
                    type="response_item",
                    payload=dict(
                        type="message",
                        role=role,
                        content=[dict(type="input_text" if role == "user" else "output_text", text=text)],
                    ),
                )
            )
    else:
        record = home / "projects/TEST-workspace" / f"{session}.jsonl"
        rows = [
            dict(
                type=role,
                uuid=f"TEST-{role}",
                timestamp=NOW,
                origin=dict(kind="human"),
                message=dict(role=role, content=text),
            )
            for role, text in [
                ("user", "TEST public <private>hidden segment</private> retained"),
                ("assistant", "TEST reply"),
            ]
        ]
        if host == "workbuddy":
            rows = [
                dict(
                    type="message",
                    id=f"TEST-{role}",
                    timestamp=NOW,
                    role=role,
                    content=[
                        dict(
                            type="input_text" if role == "user" else "output_text",
                            text=f"<user_query>{text}</user_query>" if role == "user" else text,
                        )
                    ],
                )
                for role, text in [
                    ("user", "TEST public <private>hidden segment</private> retained"),
                    ("assistant", "TEST reply"),
                ]
            ]
    record.parent.mkdir(parents=True)
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    config = load_shared_client(home, host)
    return root, config, session, record


def _sources(root):
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        return db.execute(
            "SELECT role,content FROM source_events WHERE entry_id='ledger-client' ORDER BY rowid"
        ).fetchall()


@pytest.mark.parametrize("failure", ["DEADLINE_EXCEEDED", "SCHEMA_UNSUPPORTED"])
def test_worker_recovers_rejected_hook_and_replays_idempotently(ledger_client, monkeypatch, failure):
    root, config, session, record = ledger_client
    with closing(CodexHookHandler(config)) as hook:

        def rejected(*args, **kwargs):
            raise ContractError(failure, "upgrade_in_progress" if failure == "SCHEMA_UNSUPPORTED" else None)

        monkeypatch.setattr(hook.core, "record_host_event", rejected)
        hook.handle_payload(
            dict(
                hook_event_name="Stop",
                session_id=session,
                turn_id="TEST-turn",
                prompt_id="TEST-turn",
                cwd=str(config.home),
                transcript_path=str(record),
                last_assistant_message="TEST reply",
            ),
            local_record=False,
        )
    assert not _sources(root)
    result = reconcile_store(config.to_binding(), seconds=10)
    assert result.get("completed") == 1, result
    assert _sources(root) == [("user", "TEST public  retained"), ("assistant", "TEST reply")]
    reconcile_store(config.to_binding(), seconds=10)
    assert len(_sources(root)) == 2


def test_worker_reads_session_even_when_no_end_hook_ran(ledger_client):
    root, config, _session, _record = ledger_client
    assert not _sources(root)
    reconcile_store(config.to_binding(), seconds=10)
    assert len(_sources(root)) == 2
    assert all("hidden" not in text for _role, text in _sources(root))


def test_worker_respects_closed_private_interval_on_late_replay(ledger_client):
    from types import SimpleNamespace

    from scope_recall.adapters.clients.identity import resolve_runtime_audience, stored_session_id, trusted_context
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, config, session, _record = ledger_client
    clock = SimpleNamespace(utc_now=lambda: "2026-09-24T19:59:00Z", monotonic=time.monotonic)
    core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
    context = trusted_context(config, resolve_runtime_audience(config, str(config.home)), session_id=session)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.private_on(target="session", session=stored_session_id(config, session))
    clock.utc_now = lambda: "2026-09-24T20:01:00Z"
    owner.private_off(target="session", session=stored_session_id(config, session))
    result = reconcile_store(config.to_binding(), seconds=10)
    assert result.get("completed") == 1, result
    assert not _sources(root)


def test_worker_recovers_after_hook_process_is_killed(ledger_client):
    import queue
    import subprocess
    import sys
    import threading

    root, config, session, record = ledger_client
    program = """
import json, sys, time
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.handler import CodexHookHandler
config=load_shared_client(sys.argv[1],sys.argv[2])
hook=CodexHookHandler(config)
def stalled(*a,**k):
    print("CAPTURE_ENTERED",flush=True)
    time.sleep(60)
hook.core.record_host_event=stalled
hook.handle_payload(json.loads(sys.argv[3]),local_record=False)
"""
    payload = dict(
        hook_event_name="Stop",
        session_id=session,
        turn_id="TEST-turn",
        prompt_id="TEST-turn",
        cwd=str(config.home),
        transcript_path=str(record),
        last_assistant_message="TEST reply",
    )
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", program, str(config.home), config.host, json.dumps(payload)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
        assert ready.get(timeout=15).strip() == b"CAPTURE_ENTERED"
        child.kill()
        child.wait(timeout=5)
        assert not _sources(root)
        reconcile_store(config.to_binding(), seconds=10)
        assert len(_sources(root)) == 2
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()


def test_rollout_internal_analysis_is_not_visible_evidence():
    from scope_recall.adapters.clients.transcript import codex_said

    row = dict(
        type="response_item",
        timestamp=NOW,
        payload=dict(
            type="message",
            role="assistant",
            channel="analysis",
            content=[dict(type="output_text", text="TEST internal thought")],
        ),
    )
    assert codex_said(row) is None


def test_wire_records_omit_private_spans():
    from scope_recall.adapters.clients.remote_client import _visible_payload
    from scope_recall.adapters.clients.transcript import Said, said_to_wire

    text = "TEST public <private>hidden segment</private> retained"
    assert "hidden" not in json.dumps(said_to_wire(Said("TEST-entry", "user", text, NOW)))
    assert "hidden" not in json.dumps(
        _visible_payload(dict(prompt=text, last_assistant_message=text, record=[dict(text=text)]))
    )


def test_local_rollout_is_discovered_after_hook_died_before_locator(installed, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    config, _core, _clock, project = installed
    native = tmp_path / "TEST-native-codex"
    monkeypatch.setenv("CODEX_HOME", str(native))
    record = native / "sessions/2026/11/01/rollout-TEST-local.jsonl"
    record.parent.mkdir(parents=True)
    stamp = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    rows = [
        dict(type="session_meta", timestamp=stamp, payload=dict(id="TEST-local", cwd=str(project))),
        dict(
            type="response_item",
            timestamp=stamp,
            payload=dict(type="message", role="user", content=[dict(type="input_text", text="TEST local recovered")]),
        ),
    ]
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = reconcile_store(config.to_binding(), seconds=10)
    assert result.get("completed") == 1, result
    with closing(sqlite3.connect(config.data_directory / "memory.sqlite3")) as db:
        assert db.execute("SELECT content FROM source_events").fetchall() == [("TEST local recovered",)]


def test_given_up_ledger_reopens_only_through_explicit_recovery(ledger_client):
    from scope_recall.adapters.clients.ledger import retry_given_up
    from scope_recall.adapters.clients.transcript import Cursor

    root, config, session, record = ledger_client
    cursor = Cursor(config.home, session, record)
    cursor.save(0)
    cursor.save_state(last_error_code="GAVE_UP|synthetic|25|replay|SCHEMA_UNSUPPORTED")
    assert reconcile_store(config.to_binding(), seconds=10).get("given_up") == 1
    assert not _sources(root)
    assert retry_given_up(config.to_binding(), limit=8, dry_run=True)["reopened"] == 0
    assert not _sources(root)
    assert retry_given_up(config.to_binding(), limit=8, dry_run=False)["reopened"] == 1
    reconcile_store(config.to_binding(), seconds=10)
    assert len(_sources(root)) == 2


def test_active_upgrade_lock_refuses_hook_and_ledger_recovers_after_release(ledger_client):
    import queue
    import subprocess
    import sys
    import threading

    from scope_recall.core.store_upgrade import UPGRADE_LOCK, UPGRADE_STATE, upgrade_in_progress

    root, config, session, record = ledger_client
    (root / UPGRADE_STATE).write_text(json.dumps({"status": "running"}), encoding="utf-8")
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from scope_recall.core.file_lock import advisory_file_lock\n"
        "with advisory_file_lock(Path(sys.argv[1]), timeout_seconds=0):\n"
        "    print('UPGRADE_HELD', flush=True)\n    sys.stdin.buffer.read(1)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", code, str(root / UPGRADE_LOCK)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
        assert ready.get(timeout=20).strip() == b"UPGRADE_HELD"
        assert upgrade_in_progress(root)
        with closing(CodexHookHandler(config)) as hook:
            hook.handle_payload(
                dict(
                    hook_event_name="UserPromptSubmit",
                    session_id=session,
                    turn_id="TEST-upgrade-turn",
                    prompt_id="TEST-upgrade-turn",
                    prompt="TEST public message",
                    cwd=str(config.home),
                    transcript_path=str(record),
                )
            )
            assert hook.diagnostics.capture_error_detail == "SCHEMA_UNSUPPORTED"
        assert not _sources(root)
        _out, error = child.communicate(input=b"x", timeout=10)
        assert child.returncode == 0, error
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()
    assert not upgrade_in_progress(root)
    assert reconcile_store(config.to_binding(), seconds=10).get("completed") == 1
    assert len(_sources(root)) == 2
    reconcile_store(config.to_binding(), seconds=10)
    assert len(_sources(root)) == 2


def test_uninstalled_local_client_does_not_create_a_replay_retry(tmp_path):
    from scope_recall.contracts import InstanceBinding

    binding = InstanceBinding("TEST-agent", "TEST-installation", tmp_path / "TEST-data", frozenset({"TEST-scope"}))
    assert reconcile_store(binding, seconds=1) == {}


def test_project_private_history_uses_recorded_workspace(ledger_client, tmp_path):
    from types import SimpleNamespace

    from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, config, session, record = ledger_client
    project = tmp_path / "TEST-protected-project"
    project.mkdir()
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row.get("type") == "session_meta":
            row["payload"]["cwd"] = str(project)
        elif config.host != "codex":
            row["cwd"] = str(project)
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    clock = SimpleNamespace(utc_now=lambda: "2026-09-24T19:59:00Z", monotonic=time.monotonic)
    core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
    context = trusted_context(
        config, resolve_runtime_audience(config, str(project)), session_id=session, project_root=str(project)
    )
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.private_on(target="project", project=str(project))
    clock.utc_now = lambda: "2026-09-24T20:01:00Z"
    owner.private_off(target="project", project=str(project))
    reconcile_store(config.to_binding(), seconds=10)
    assert not _sources(root)


def test_codex_context_only_page_keeps_workspace_for_later_messages(ledger_client, tmp_path):
    from types import SimpleNamespace

    from scope_recall.adapters.clients import transcript
    from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, config, session, record = ledger_client
    if config.host != "codex":
        pytest.skip("context records belong to the Codex format")
    protected = tmp_path / "TEST-resumed-project"
    protected.mkdir()
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    rows.insert(1, dict(type="turn_context", timestamp=NOW, payload=dict(cwd=str(protected))))
    encoded = [(json.dumps(row) + "\n").encode() for row in rows]
    record.write_bytes(b"".join(encoded))
    page = transcript.read(record, 0, limit=sum(map(len, encoded[:2])), rows=transcript.codex_said)
    assert all(entry is None for _end, entry in page)
    through = page[-1][0]
    cursor = transcript.Cursor(config.home, session, record)
    cursor.save(through, directory=page.directory_at(through))
    assert cursor.directory() is not None
    clock = SimpleNamespace(utc_now=lambda: "2026-09-24T19:59:00Z", monotonic=time.monotonic)
    core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
    context = trusted_context(
        config, resolve_runtime_audience(config, str(protected)), session_id=session, project_root=str(protected)
    )
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.private_on(target="project", project=str(protected))
    clock.utc_now = lambda: "2026-09-24T20:01:00Z"
    owner.private_off(target="project", project=str(protected))
    reconcile_store(config.to_binding(), seconds=10)
    assert not _sources(root)


def test_unknown_record_workspace_cannot_bypass_project_private_history(ledger_client, tmp_path):
    from types import SimpleNamespace

    from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, config, session, record = ledger_client
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row.pop("cwd", None)
        if row.get("type") == "session_meta":
            row["payload"].pop("cwd", None)
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    protected = tmp_path / "TEST-private-unknown-origin"
    protected.mkdir()
    clock = SimpleNamespace(utc_now=lambda: "2026-09-24T19:59:00Z", monotonic=time.monotonic)
    core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
    context = trusted_context(
        config, resolve_runtime_audience(config, str(protected)), session_id=session, project_root=str(protected)
    )
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.private_on(target="project", project=str(protected))
    clock.utc_now = lambda: "2026-09-24T20:01:00Z"
    owner.private_off(target="project", project=str(protected))
    reconcile_store(config.to_binding(), seconds=10)
    assert not _sources(root)


def test_unrecognized_metadata_kind_does_not_stop_a_rollout_page(tmp_path):
    from scope_recall.adapters.clients.transcript import codex_said, read

    record = tmp_path / "TEST-malformed-envelope.jsonl"
    rows = [
        dict(type="session_meta", payload=dict(id="TEST-meta", cwd=str(tmp_path))),
        dict(type=[], payload={}),
        dict(
            type="response_item",
            timestamp=NOW,
            payload=dict(
                type="message",
                role="user",
                content=[dict(type="input_text", text="TEST retained message")],
            ),
        ),
    ]
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    page = read(record, 0, rows=codex_said)
    assert [entry.text for _end, entry in page if entry is not None] == ["TEST retained message"]
