"""Replay identity, authorization and bounded scans use synthetic client records only."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from scope_recall.adapters.clients import ledger
from scope_recall.adapters.clients.handler import CodexHookHandler

from tests.host.codex.test_capture_ledger import _sources, ledger_client
from tests.host.codex.test_shared_client import store


@pytest.fixture
def renamed_workbuddy(ledger_client):
    root, config, session, record = ledger_client
    if config.host != "workbuddy":
        pytest.skip("WorkBuddy record identity")
    rows = [dict(json.loads(line), sessionId=session) for line in record.read_text(encoding="utf-8").splitlines()]
    target = record.with_name("TEST-agent-store.jsonl")
    record.rename(target)
    target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return root, config, session, target


def test_workbuddy_agent_filename_obeys_real_session_private_interval(renamed_workbuddy):
    from scope_recall.adapters.clients.identity import resolve_runtime_audience, stored_session_id, trusted_context
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, config, session, _record = renamed_workbuddy
    clock = SimpleNamespace(utc_now=lambda: "2026-09-24T19:59:00Z", monotonic=time.monotonic)
    core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
    context = trusted_context(config, resolve_runtime_audience(config, str(config.home)), session_id=session)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.private_on(target="session", session=stored_session_id(config, session))
    clock.utc_now = lambda: "2026-09-24T20:01:00Z"
    owner.private_off(target="session", session=stored_session_id(config, session))
    ledger.reconcile_store(config.to_binding(), seconds=10)
    assert not _sources(root)


@pytest.mark.parametrize("identity", [None, "conflicting"])
def test_workbuddy_replay_refuses_missing_or_conflicting_identity(renamed_workbuddy, identity):
    root, config, _session, record = renamed_workbuddy
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    if identity is None:
        for row in rows:
            row.pop("sessionId")
    else:
        rows[-1]["sessionId"] = "TEST-another-session"
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    ledger.reconcile_store(config.to_binding(), seconds=10)
    assert not _sources(root)


def test_workbuddy_agent_filename_replay_deduplicates_original_capture(renamed_workbuddy):
    root, config, session, record = renamed_workbuddy
    with closing(CodexHookHandler(config)) as hook:
        hook.handle_payload(
            dict(
                hook_event_name="Stop",
                session_id=session,
                agent_id=record.stem,
                cwd=str(config.home),
                transcript_path=str(record),
            ),
            local_record=True,
        )
    assert len(_sources(root)) == 2
    from scope_recall.adapters.clients.transcript import Cursor

    Cursor(config.home, session, record).save(0)
    ledger.reconcile_store(config.to_binding(), seconds=10)
    ledger.reconcile_store(config.to_binding(), seconds=10)
    assert len(_sources(root)) == 2


def test_shared_replay_budget_resumes_across_files_and_entries(ledger_client, monkeypatch):
    _root, config, _session, _record = ledger_client
    second = replace(
        config, manifest=replace(config.manifest, entry_id="TEST-second-entry"), home=config.home / "TEST-second"
    )
    monkeypatch.setattr(ledger, "shared_clients", lambda binding: [(config, "2026-01-01Z"), (second, "2026-01-01Z")])
    monkeypatch.setattr(ledger, "_records", lambda client: iter([client.home / "a.jsonl", client.home / "b.jsonl"]))
    _assert_scan_resumes(config.to_binding(), monkeypatch)


def test_local_replay_budget_resumes_across_files(installed, tmp_path, monkeypatch):
    config, _core, _clock, _project = installed
    native = tmp_path / "TEST-native"
    monkeypatch.setenv("CODEX_HOME", str(native))
    directory = native / "sessions/2026/01/01"
    directory.mkdir(parents=True)
    for index in range(4):
        (directory / f"rollout-TEST-{index}.jsonl").write_text("{}\n", encoding="utf-8")
    _assert_scan_resumes(config.to_binding(), monkeypatch)


def _assert_scan_resumes(binding, monkeypatch):
    ticks = [0.0]
    seen = []
    monkeypatch.setattr(ledger, "time", SimpleNamespace(monotonic=lambda: ticks[0]))

    def completed(config, record, attached, deadline):
        seen.append(str(record))
        ticks[0] += 2
        return "completed"

    monkeypatch.setattr(ledger, "_reconcile", completed)
    for _index in range(3):
        assert ledger.reconcile_store(binding, seconds=1).get("pending_scan") == 1
    assert not ledger.reconcile_store(binding, seconds=1).get("pending_scan")
    assert len(seen) == len(set(seen)) == 4


def test_config_resave_does_not_move_legacy_capture_authorization(installed, tmp_path, monkeypatch):
    config, core, clock, project = installed
    (config.data_directory / "capture-ledgers" / "authorized-at.json").unlink(missing_ok=True)
    with closing(CodexHookHandler(config, core=core, clock=clock)) as hook:
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-seed",
                turn_id="TEST-seed",
                cwd=str(project),
                prompt="TEST first authorized capture",
            )
        )
    native = tmp_path / "TEST-native"
    monkeypatch.setenv("CODEX_HOME", str(native))
    record = native / "sessions/2026/09/06/rollout-TEST-missed.jsonl"
    record.parent.mkdir(parents=True)
    rows = [
        dict(type="session_meta", payload=dict(id="TEST-missed", cwd=str(project))),
        dict(
            type="response_item",
            timestamp="2026-09-06T12:01:00Z",
            payload=dict(type="message", role="user", content=[dict(type="input_text", text="TEST missed capture")]),
        ),
    ]
    record.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    os.utime(config.config_path, (2000000000, 2000000000))
    ledger.reconcile_store(config.to_binding(), seconds=10)
    with closing(sqlite3.connect(config.data_directory / "memory.sqlite3")) as db:
        assert ("TEST missed capture",) in db.execute("SELECT content FROM source_events").fetchall()


def test_workbuddy_agent_filename_cannot_resurrect_deleted_original_messages(renamed_workbuddy):
    from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    root, config, session, record = renamed_workbuddy
    with closing(CodexHookHandler(config)) as hook:
        hook.handle_payload(
            dict(
                hook_event_name="Stop",
                session_id=session,
                agent_id=record.stem,
                cwd=str(config.home),
                transcript_path=str(record),
            ),
            local_record=True,
        )
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        refs = [row[0] for row in db.execute("SELECT event_id FROM source_events WHERE entry_id='ledger-client'")]
    assert len(refs) == 2
    core = MemoryCore(CoreConfig(config.to_binding()))
    context = trusted_context(config, resolve_runtime_audience(config, str(config.home)), session_id=session)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.apply(owner.preview(refs))
    assert owner.list()["items"] == []
    from scope_recall.adapters.clients.transcript import Cursor

    Cursor(config.home, session, record).save(0)
    ledger.reconcile_store(config.to_binding(), seconds=10)
    assert owner.list()["items"] == []
    assert len(_sources(root)) == len(refs)


def test_worker_schedules_budget_truncated_replay(tmp_path, monkeypatch):
    from io import StringIO

    from scope_recall.adapters.clients import store_service
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.runtime import worker_entry

    from tests.contract.test_runtime_worker_entry import _binding, _config_payload, _write_config

    binding = _binding(tmp_path / "TEST-data")
    MemoryCore(CoreConfig(binding)).initialize()
    config = _write_config(tmp_path / "TEST-worker.json", _config_payload(binding))
    monkeypatch.setattr(ledger, "reconcile_store", lambda *a, **k: {"completed": 1, "pending_scan": 1})
    monkeypatch.setattr(store_service, "manage", lambda *a, **k: None)
    output = StringIO()
    assert worker_entry.run_worker(config, output=output) == 0, output.getvalue()
    checkpoint = binding.data_directory / "transcript-replay.json"
    assert checkpoint.exists()
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["next_at"] is not None


@pytest.mark.parametrize("basis", ["receipt", "first_capture", "none"])
def test_legacy_authorization_uses_only_reliable_evidence(installed, basis):
    from datetime import UTC, datetime

    from scope_recall.adapters.clients import ledger_state
    from scope_recall.adapters.installation_receipt import RECEIPT_FILENAME, RECEIPT_SCHEMA, receipt_digest

    config, core, clock, project = installed
    path = config.data_directory / "capture-ledgers" / "authorized-at.json"
    path.unlink()
    if basis == "first_capture":
        with closing(CodexHookHandler(config, core=core, clock=clock)) as hook:
            hook.handle_payload(
                dict(
                    hook_event_name="UserPromptSubmit",
                    session_id="TEST-seed",
                    turn_id="TEST-seed",
                    cwd=str(project),
                    prompt="TEST reliable initial capture",
                )
            )
    elif basis == "receipt":
        receipt = dict(
            schema_version=RECEIPT_SCHEMA,
            installation_id=config.installation_id,
            host="codex",
            instance_root=os.path.normcase(str(config.config_path.parent.resolve())),
        )
        receipt["receipt_sha256"] = receipt_digest(receipt)
        receipt_path = config.config_path.parent / RECEIPT_FILENAME
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        stamp = datetime.fromisoformat(clock.now.replace("Z", "+00:00")).timestamp()
        os.utime(receipt_path, (stamp, stamp))
    os.utime(config.config_path, (2000000000, 2000000000))
    first = ledger_state.local_authorized_at(config, seconds=5)
    if basis == "none":
        assert first is None
        assert not path.exists()
    else:
        assert datetime.fromisoformat(first) == datetime.fromisoformat(clock.now.replace("Z", "+00:00"))
        os.utime(config.config_path, (2100000000, 2100000000))
        assert ledger_state.local_authorized_at(config, seconds=5) == first
        assert json.loads(path.read_text(encoding="utf-8"))["basis"] == (
            "installation" if basis == "receipt" else basis
        )


def test_authorization_never_infers_a_timezone_from_checkpoint(installed):
    from scope_recall.adapters.clients import ledger_state

    config, _core, _clock, _project = installed
    ledger_state.save_checkpoint(
        config.data_directory,
        "authorized-at.json",
        dict(installation_id=config.installation_id, since="2026-01-01T12:00:00"),
    )
    with pytest.raises(ValueError, match="timezone"):
        ledger_state.local_authorized_at(config, seconds=5)


def test_budgeted_replay_eventually_captures_all_files_and_entries(ledger_client, tmp_path, monkeypatch):
    from scope_recall.adapters.clients.config import load_shared_client
    from scope_recall.adapters.shared_entries import attach_shared_record, client_entry_record

    from tests.host.codex.test_shared_client import NOW

    root, config, session, record = ledger_client
    other_home = tmp_path / "TEST-other-client"
    scope = next(iter(config.scope_ids))
    attach_shared_record(
        root,
        client_entry_record(
            host=config.host,
            home=other_home,
            entry_id="ledger-second",
            display_name="Synthetic second ledger",
            attached_at=NOW,
            allowed_scope_ids=[scope],
            writable_scope_ids=[scope],
            capture_scope_id=scope,
        ),
        now=NOW,
    )
    other = load_shared_client(other_home, config.host)
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    for index, client in enumerate([config, other, other], start=1):
        target = (
            client.home
            / record.relative_to(config.home).parent
            / (f"rollout-TEST-{index}.jsonl" if config.host == "codex" else f"TEST-session-{index}.jsonl")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = []
        for original in rows:
            row = dict(original)
            row["sessionId"] = f"TEST-session-{index}"
            if row.get("type") == "session_meta":
                row["payload"] = dict(row["payload"], id=f"TEST-session-{index}")
            copied.append(row)
        target.write_text("".join(json.dumps(row) + "\n" for row in copied), encoding="utf-8")
    original = ledger._reconcile
    ticks = [0.0]
    monkeypatch.setattr(ledger, "time", SimpleNamespace(monotonic=lambda: ticks[0]))

    def page(client, path, attached, deadline):
        state = original(client, path, attached, time.monotonic() + 10)
        ticks[0] += 2
        return state

    monkeypatch.setattr(ledger, "_reconcile", page)
    for _index in range(3):
        assert ledger.reconcile_store(config.to_binding(), seconds=1).get("pending_scan") == 1
    assert not ledger.reconcile_store(config.to_binding(), seconds=1).get("pending_scan")
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        assert db.execute(
            "SELECT count(*) FROM source_events WHERE entry_id IN ('ledger-client','ledger-second')"
        ).fetchone() == (8,)


def test_reinstall_preserves_legacy_capture_authorization(installed):
    from datetime import datetime

    from scope_recall.adapters.clients.config import install_codex_scope_recall
    from scope_recall.adapters.clients.ledger_state import local_authorized_at

    config, core, clock, project = installed
    (config.data_directory / "capture-ledgers" / "authorized-at.json").unlink()
    with closing(CodexHookHandler(config, core=core, clock=clock)) as hook:
        hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-seed",
                turn_id="TEST-seed",
                cwd=str(project),
                prompt="TEST first authorized capture",
            )
        )
    first_capture = clock.now
    clock.now = "2026-09-07T12:00:00Z"
    reinstalled, _core = install_codex_scope_recall(config.config_path.parent, project_root=project, clock=clock)
    assert datetime.fromisoformat(local_authorized_at(reinstalled, seconds=5)) == datetime.fromisoformat(
        first_capture.replace("Z", "+00:00")
    )


def test_fresh_store_does_not_inherit_purged_store_authorization(installed):
    from datetime import datetime

    from scope_recall.adapters.clients.config import install_codex_scope_recall
    from scope_recall.adapters.clients.ledger_state import local_authorized_at

    config, _core, clock, project = installed
    config.config_path.unlink()
    (config.data_directory / "memory.sqlite3").unlink()
    clock.now = "2026-09-07T12:00:00Z"
    fresh, _core = install_codex_scope_recall(config.config_path.parent, project_root=project, clock=clock)
    assert datetime.fromisoformat(local_authorized_at(fresh, seconds=5)) == datetime.fromisoformat(
        clock.now.replace("Z", "+00:00")
    )
