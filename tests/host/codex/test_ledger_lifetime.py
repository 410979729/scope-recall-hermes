"""Capture recovery follows store lifetimes and rotates unfinished records fairly."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from scope_recall.adapters.clients import ledger, transcript
from scope_recall.adapters.clients.config import install_codex_scope_recall, load_codex_config
from scope_recall.adapters.clients.ledger_state import local_authorized_at, save_checkpoint


def _native(tmp_path, monkeypatch):
    home = tmp_path / "TEST-native"
    directory = home / "sessions/2026/09/06"
    directory.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return directory


def _message(text):
    return dict(
        type="response_item",
        timestamp="2026-09-06T12:01:00Z",
        payload=dict(type="message", role="user", content=[dict(type="input_text", text=text)]),
    )


def _record(directory, name, project, *, partial=False):
    path = directory / f"rollout-{name}.jsonl"
    rows = [dict(type="session_meta", payload=dict(id=f"TEST-{name}", cwd=str(project))), _message(f"TEST {name}")]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if partial:
        with path.open("ab") as stream:
            stream.write(b'{"type":"response_item"')
    return path


def test_rebuilt_database_preserving_config_starts_new_authorization(installed):
    config, core, clock, project = installed
    old = local_authorized_at(config, seconds=5)
    save_checkpoint(
        config.data_directory, "scan.json", dict(installation_id=config.installation_id, after="TEST-stale")
    )
    (config.data_directory / "memory.sqlite3").unlink()
    clock.now = "2026-11-01T12:00:00Z"
    rebuilt, _core = install_codex_scope_recall(config.config_path.parent, project_root=project, clock=clock)
    current = local_authorized_at(rebuilt, seconds=5)
    assert current != old
    assert datetime.fromisoformat(current) == datetime.fromisoformat(clock.now.replace("Z", "+00:00"))
    state = json.loads((config.data_directory / "capture-ledgers/scan.json").read_text(encoding="utf-8"))
    assert state["after"] is None


def test_partial_tail_does_not_starve_later_record(installed, tmp_path, monkeypatch):
    config, _core, _clock, project = installed
    directory = _native(tmp_path, monkeypatch)
    _record(directory, "a", project, partial=True)
    _record(directory, "b", project)
    result = ledger.reconcile_store(config.to_binding(), seconds=10)
    with closing(sqlite3.connect(config.data_directory / "memory.sqlite3")) as db:
        texts = [row[0] for row in db.execute("SELECT content FROM source_events")]
    assert "TEST b" in texts, result
    assert result.get("pending_scan"), result


def test_growing_record_rotates_across_budget_to_later_record(installed, tmp_path, monkeypatch):
    config, _core, _clock, project = installed
    directory = _native(tmp_path, monkeypatch)
    first = _record(directory, "a", project)
    _record(directory, "b", project)
    read = transcript.read
    ticks = [0.0]
    monkeypatch.setattr(ledger, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    reconcile = ledger._reconcile

    def growing(path, offset, **kwargs):
        page = read(path, offset, **kwargs)
        if path == first:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(_message("TEST appended")) + "\n")
        return page

    def bounded(client, path, since, deadline):
        state = reconcile(client, path, since, time.monotonic() + 10)
        ticks[0] += 2
        return state

    monkeypatch.setattr(transcript, "read", growing)
    monkeypatch.setattr(ledger, "_reconcile", bounded)
    for _index in range(3):
        ledger.reconcile_store(config.to_binding(), seconds=1)
    with closing(sqlite3.connect(config.data_directory / "memory.sqlite3")) as db:
        texts = [row[0] for row in db.execute("SELECT content FROM source_events")]
    assert "TEST b" in texts


def test_deferred_revisit_survives_last_budget_segment(installed, tmp_path, monkeypatch):
    from scope_recall.contracts import ContractError
    from scope_recall.core.inbox_rules import deferral

    config, _core, _clock, project = installed
    directory = _native(tmp_path, monkeypatch)
    first = _record(directory, "a", project)
    _record(directory, "b", project)
    ticks = [0.0]
    monkeypatch.setattr(ledger, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    seen = []

    def outcome(client, path, since, deadline):
        seen.append(path.name)
        ticks[0] += 2
        if path == first:
            cursor = transcript.Cursor(config.data_directory, "TEST-a", first)
            cursor.save(0)
            cursor.save_state(
                last_error_code=deferral(None, ContractError("DEADLINE_EXCEEDED"), datetime.now(UTC), path="replay")
            )
            return "deferred"
        return "completed"

    monkeypatch.setattr(ledger, "_reconcile", outcome)
    first_pass = ledger.reconcile_store(config.to_binding(), seconds=1)
    assert first_pass.get("deferred") and first_pass.get("pending_scan")
    last_pass = ledger.reconcile_store(config.to_binding(), seconds=1)
    assert seen == ["rollout-a.jsonl", "rollout-b.jsonl"]
    assert last_pass.get("pending_scan") or last_pass.get("deferred"), last_pass


def test_apply_install_preserves_old_receipt_before_rewrite(tmp_path):
    from scope_recall.maintenance.install import apply_install, plan_install
    from scope_recall.maintenance.install_common import RECEIPT_FILENAME

    instance = tmp_path / "TEST-instance"
    plugin = tmp_path / "TEST-plugins" / "scope-recall"
    plugin.mkdir(parents=True)
    project = tmp_path / "TEST-project"
    project.mkdir()

    def install():
        return apply_install(
            plan_install(
                host="codex",
                instance_root=instance,
                target_plugin_dir=plugin,
                project_root=project,
                agent_id="TEST-agent",
                python_executable=Path(sys.executable),
                test_mode=True,
            )
        )

    install()
    for _attempt in range(2):
        config = load_codex_config(instance / "codex-installation.json")
        (config.data_directory / "capture-ledgers/authorized-at.json").unlink()
        expected = datetime.fromtimestamp((instance / RECEIPT_FILENAME).stat().st_mtime, UTC)
        time.sleep(0.03)
        install()
        actual = local_authorized_at(config, seconds=5)
        assert datetime.fromisoformat(actual) == expected


def test_invalid_authorization_stops_and_doctor_explains_safe_repair(installed, tmp_path, monkeypatch):
    from scope_recall.maintenance.doctor import run_doctor

    config, _core, _clock, project = installed
    directory = _native(tmp_path, monkeypatch)
    _record(directory, "a", project)
    path = config.data_directory / "capture-ledgers/authorized-at.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["since"] = "not-a-time"
    path.write_text(json.dumps(state), encoding="utf-8")
    result = ledger.reconcile_store(config.to_binding(), seconds=5)
    assert result.get("stopped") == 1 and not result.get("deferred"), result
    report = run_doctor(host="codex", instance_root=config.config_path.parent)
    checks = [check for check in report.checks if check["name"] == "capture_authorization"]
    assert checks and checks[0]["result"] == "blocked"
    assert "time" in checks[0]["detail"] and "install" in checks[0]["detail"]
    assert json.loads(path.read_text(encoding="utf-8"))["since"] == "not-a-time"


def test_core_rebuild_changes_database_lifetime_even_with_config_retained(installed):
    config, core, clock, _project = installed
    path = config.data_directory / "capture-ledgers/authorized-at.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    save_checkpoint(
        config.data_directory,
        "scan.json",
        dict(installation_id=config.installation_id, database_id=old["database_id"], after="TEST-stale", revisit=True),
    )
    (config.data_directory / "memory.sqlite3").unlink()
    clock.now = "2026-11-01T12:00:00Z"
    core.initialize()
    assert datetime.fromisoformat(local_authorized_at(config, seconds=5)) == datetime.fromisoformat(
        clock.now.replace("Z", "+00:00")
    )
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["database_id"] != old["database_id"]
    scan = json.loads((config.data_directory / "capture-ledgers/scan.json").read_text(encoding="utf-8"))
    assert scan["database_id"] == current["database_id"] and scan["after"] is None and not scan["revisit"]


def test_database_file_replacement_preserves_logical_lifetime(installed):
    import shutil

    config, core, _clock, _project = installed
    path = config.data_directory / "capture-ledgers/authorized-at.json"
    before = json.loads(path.read_text(encoding="utf-8"))
    copy = config.data_directory / "TEST-database-copy.sqlite3"
    shutil.copyfile(core.storage.path, copy)
    copy.replace(core.storage.path)
    core.initialize()
    assert local_authorized_at(config, seconds=5) == before["since"]
    assert json.loads(path.read_text(encoding="utf-8"))["database_id"] == before["database_id"]


def test_legacy_database_upgrade_binds_identity_without_guessing_birth(installed):
    from v11_support import downgrade_store

    config, core, _clock, _project = installed
    path = config.data_directory / "capture-ledgers/authorized-at.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old.pop("database_id")
    path.write_text(json.dumps(old), encoding="utf-8")
    downgrade_store(core.storage.path, 1112)
    core.initialize()
    assert local_authorized_at(config, seconds=5) == old["since"]
    with closing(sqlite3.connect(core.storage.path)) as db:
        identity, born = db.execute("SELECT database_id,database_created_at FROM instance_meta").fetchone()
    assert len(identity) == 32 and born is None


def test_trusted_installer_repairs_invalid_time_with_future_only_grant(tmp_path, monkeypatch):
    from scope_recall.maintenance.install import apply_install, plan_install

    instance = tmp_path / "TEST-instance"
    plugin = tmp_path / "TEST-plugins/scope-recall"
    plugin.mkdir(parents=True)
    project = tmp_path / "TEST-project"
    project.mkdir()

    def install():
        apply_install(
            plan_install(
                host="codex",
                instance_root=instance,
                target_plugin_dir=plugin,
                project_root=project,
                agent_id="TEST-agent",
                python_executable=Path(sys.executable),
                test_mode=True,
            )
        )

    install()
    config = load_codex_config(instance / "codex-installation.json")
    path = config.data_directory / "capture-ledgers/authorized-at.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["since"] = "not-a-time"
    path.write_text(json.dumps(value), encoding="utf-8")
    before = datetime.now(UTC)
    install()
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["basis"] == "explicit_reauthorization"
    assert datetime.fromisoformat(repaired["since"]) >= before
    directory = _native(tmp_path, monkeypatch)
    _record(directory, "old", project)
    ledger.reconcile_store(config.to_binding(), seconds=10)
    with closing(sqlite3.connect(config.data_directory / "memory.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM source_events").fetchone() == (0,)


def test_worker_keeps_deferred_revisit_until_original_backoff_is_due(installed, tmp_path, monkeypatch):
    from io import StringIO

    from scope_recall.adapters.clients import store_service
    from scope_recall.contracts import ContractError
    from scope_recall.core import MemoryCore
    from scope_recall.runtime import worker_entry

    from tests.contract.test_runtime_worker_entry import _config_payload, _write_config

    config, _core, _clock, project = installed
    directory = _native(tmp_path, monkeypatch)
    _record(directory, "a", project)
    _record(directory, "b", project)
    worker = _write_config(tmp_path / "TEST-worker.json", _config_payload(config.to_binding()))
    monkeypatch.setattr(store_service, "manage", lambda *a, **k: None)
    ticks = [0.0]
    moment = [datetime.now(UTC)]
    attempts = []
    original = ledger._reconcile
    record = MemoryCore.record_event

    class ClockDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment[0]

    def transient(self, context, value, **kwargs):
        if value.get("content") == "TEST a":
            attempts.append(moment[0])
            if len(attempts) == 1:
                raise ContractError("DEADLINE_EXCEEDED")
        return record(self, context, value, **kwargs)

    def bounded(client, path, since, deadline):
        state = original(client, path, since, time.monotonic() + 10)
        ticks[0] += 3
        return state

    monkeypatch.setattr(ledger, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    monkeypatch.setattr(ledger, "datetime", ClockDate)
    monkeypatch.setattr(ledger, "_reconcile", bounded)
    monkeypatch.setattr(MemoryCore, "record_event", transient)
    checkpoint = config.data_directory / "transcript-replay.json"
    for _index in range(4):
        output = StringIO()
        assert worker_entry.run_worker(worker, output=output) == 0, output.getvalue()
        assert json.loads(checkpoint.read_text(encoding="utf-8"))["next_at"] is not None
    assert len(attempts) == 1
    moment[0] += timedelta(seconds=61)
    for _index in range(2):
        assert worker_entry.run_worker(worker, output=StringIO()) == 0
    assert len(attempts) == 2
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["next_at"] is None
    with closing(sqlite3.connect(config.data_directory / "memory.sqlite3")) as db:
        assert {row[0] for row in db.execute("SELECT content FROM source_events")} == {"TEST a", "TEST b"}
