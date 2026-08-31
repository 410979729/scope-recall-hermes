from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from scope_recall import activation_transaction, installer
from scope_recall.maintenance_lease import (
    ACTIVATION_GUARD_TRIGGER_PREFIX,
    acquire_activation_lease,
    activation_lease_path,
)
from scope_recall.sql_store import ensure_schema


def _candidate_target(home: Path, *, version: str = "1.10.3") -> Path:
    target = home / "plugins" / "scope-recall"
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.yaml").write_text(
        f"name: scope-recall\nversion: {version}\n",
        encoding="utf-8",
    )
    return target


def _private_state(home: Path, operation_id: str = "op-1") -> Path:
    path = home / "scope-recall" / "upgrades" / "operations" / operation_id / "private"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot(home: Path) -> dict[str, Any]:
    storage = home / "scope-recall"
    return {
        "snapshot_root": home / "backups" / "activation",
        "storage_dir": storage,
        "storage_dir_preexisting": True,
        "maintenance_lease": {},
        "config": {"path": home / "config.yaml", "kind": "absent"},
        "storage_config": {"path": storage / "config.json", "kind": "absent"},
        "sqlite": {
            "path": storage / "memory.sqlite3",
            "preexisting": False,
            "backup_path": None,
            "expected_fingerprint": "absent",
            "snapshot_fingerprint": "absent",
            "writer_quiesced": True,
        },
        "vector_companions": [],
    }


def _rolled_back_receipt() -> dict[str, Any]:
    return {
        "status": "rolled_back",
        "automatic_rollback": True,
        "failures": [],
        "restore_commands": [],
    }


def test_managed_install_seals_snapshot_before_activation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state_dir = _private_state(home)
    _candidate_target(home)
    snapshot = _snapshot(home)
    observed: dict[str, Any] = {}

    monkeypatch.setattr(installer, "_is_same_tree", lambda _source, _target: True)
    monkeypatch.setattr(
        installer,
        "_upgrade_compatibility_preflight",
        lambda *_args, **_kwargs: {
            "ok": True,
            "requires_vector_degrade": False,
            "failures": [],
        },
    )
    monkeypatch.setattr(
        installer,
        "capture_activation_state",
        lambda *_args, **_kwargs: snapshot,
    )

    def fake_activate(*_args, **kwargs):
        private = kwargs["managed_state_dir"]
        transaction = installer._read_managed_transaction(private)
        observed.update(transaction)
        result = dict(kwargs["result"])
        result.update({"ok": True, "mode": "test", "safe_to_restart_previous": False})
        return result

    monkeypatch.setattr(installer, "_activate_installed_target", fake_activate)
    result = installer.install(
        home,
        activate=True,
        maintenance_mode=True,
        managed_upgrade=True,
        managed_state_dir=state_dir,
    )

    assert result["ok"] is True
    assert observed["phase"] == "snapshot_captured"
    assert observed["snapshot"]["sqlite"]["path"] == str(
        home / "scope-recall" / "memory.sqlite3"
    )
    assert observed["sha256"]


def test_resume_snapshot_only_transaction_rolls_back_and_becomes_terminal(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = _candidate_target(home)
    state_dir = _private_state(home)
    installer._begin_managed_transaction(
        state_dir,
        home=home.resolve(),
        target=target.resolve(),
        snapshot=_snapshot(home),
        previous_plugin_existed=True,
        previous_version="1.10.3",
        target_version="2.0.1",
        requires_vector_degrade=False,
    )
    monkeypatch.setattr(
        installer,
        "compensate_activation_failure",
        lambda *_args, **_kwargs: _rolled_back_receipt(),
    )
    monkeypatch.setattr(installer, "verify", lambda _home: {"ok": True})

    result = installer.resume_managed_upgrade(managed_state_dir=state_dir)
    terminal = installer._read_managed_transaction(state_dir)

    assert result["mode"] == "managed-resume-rolled-back"
    assert result["safe_to_restart_previous"] is True
    assert terminal["phase"] == "rolled_back"
    assert terminal["last_transaction"]["status"] == "rolled_back"


def test_resume_candidate_continues_activation_instead_of_guessing(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = _candidate_target(home, version="2.0.1")
    state_dir = _private_state(home)
    snapshot = _snapshot(home)
    installer._begin_managed_transaction(
        state_dir,
        home=home.resolve(),
        target=target.resolve(),
        snapshot=snapshot,
        previous_plugin_existed=True,
        previous_version="1.10.3",
        target_version="2.0.1",
        requires_vector_degrade=True,
    )
    installer._advance_managed_transaction(
        state_dir,
        "candidate_installed",
        snapshot=snapshot,
        plugin_backup_path=str(home / "backups" / "old-plugin"),
        plugin_replaced=True,
    )
    observed: dict[str, Any] = {}

    def fake_activate(*_args, **kwargs):
        observed.update(kwargs)
        return {"ok": True, "mode": "continued"}

    monkeypatch.setattr(installer, "_activate_installed_target", fake_activate)
    result = installer.resume_managed_upgrade(managed_state_dir=state_dir)

    assert result == {"ok": True, "mode": "continued"}
    assert observed["managed_upgrade"] is True
    assert observed["degrade_vector"] is True
    assert observed["plugin_replaced"] is True


def test_resume_refuses_tampered_transaction(tmp_path):
    home = tmp_path / "home"
    target = _candidate_target(home)
    state_dir = _private_state(home)
    installer._begin_managed_transaction(
        state_dir,
        home=home.resolve(),
        target=target.resolve(),
        snapshot=_snapshot(home),
        previous_plugin_existed=True,
        previous_version="1.10.3",
        target_version="2.0.1",
        requires_vector_degrade=False,
    )
    path = state_dir / "activation-transaction.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["phase"] = "committed"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(installer.InstallError, match="integrity"):
        installer.resume_managed_upgrade(managed_state_dir=state_dir)


def test_managed_journal_start_failure_releases_snapshot_barrier(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    state_dir = _private_state(home)
    _candidate_target(home)
    monkeypatch.setattr(installer, "_is_same_tree", lambda _source, _target: True)
    monkeypatch.setattr(
        installer,
        "_upgrade_compatibility_preflight",
        lambda *_args, **_kwargs: {
            "ok": True,
            "requires_vector_degrade": False,
            "failures": [],
        },
    )
    monkeypatch.setattr(
        installer,
        "capture_activation_state",
        lambda *_args, **_kwargs: _snapshot(home),
    )
    monkeypatch.setattr(
        installer,
        "_begin_managed_transaction_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    monkeypatch.setattr(
        installer,
        "compensate_activation_failure",
        lambda *_args, **_kwargs: _rolled_back_receipt(),
    )

    result = installer.install(
        home,
        activate=True,
        maintenance_mode=True,
        managed_upgrade=True,
        managed_state_dir=state_dir,
    )

    assert result["ok"] is False
    assert result["mode"] == "managed-journal-start-failed-safe"
    assert result["safe_to_restart_previous"] is True


def test_resume_snapshot_pending_removes_only_precommitted_lease_and_guards(
    tmp_path,
):
    home = tmp_path / "home"
    target = _candidate_target(home)
    state_dir = _private_state(home)
    storage = home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    db_path = storage / "memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        ensure_schema(connection)
        connection.commit()

    token = "a" * 32
    installer._begin_managed_transaction_intent(
        state_dir,
        home=home.resolve(),
        target=target.resolve(),
        previous_plugin_existed=True,
        previous_version="1.10.3",
        target_version="2.0.1",
        requires_vector_degrade=False,
        capture_lease_token=token,
    )
    acquire_activation_lease(db_path, capability_token=token)
    activation_transaction._install_sqlite_activation_guards(
        db_path,
        lease_token=token,
    )

    result = installer.resume_managed_upgrade(managed_state_dir=state_dir)
    terminal = installer._read_managed_transaction(state_dir)

    assert result["mode"] == "managed-snapshot-intent-recovered"
    assert result["safe_to_restart_previous"] is True
    assert terminal["phase"] == "rolled_back"
    assert not activation_lease_path(db_path).exists()
    with sqlite3.connect(db_path) as connection:
        guards = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE ?",
            (f"{ACTIVATION_GUARD_TRIGGER_PREFIX}%",),
        ).fetchall()
    assert guards == []


def test_resume_commit_started_finishes_cleanup_without_rollback(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = _candidate_target(home, version="2.0.1")
    state_dir = _private_state(home)
    snapshot = _snapshot(home)
    installer._begin_managed_transaction(
        state_dir,
        home=home.resolve(),
        target=target.resolve(),
        snapshot=snapshot,
        previous_plugin_existed=True,
        previous_version="1.10.3",
        target_version="2.0.1",
        requires_vector_degrade=False,
    )
    installer._advance_managed_transaction(
        state_dir,
        "commit_started",
        snapshot=snapshot,
        plugin_backup_path=str(home / "backups" / "old-plugin"),
        plugin_replaced=True,
    )
    rollback_calls = 0

    def forbidden_rollback(*_args, **_kwargs):
        nonlocal rollback_calls
        rollback_calls += 1
        raise AssertionError("durable commit decision must never roll back")

    monkeypatch.setattr(installer, "compensate_activation_failure", forbidden_rollback)
    monkeypatch.setattr(
        installer,
        "committed_activation_receipt",
        lambda *_args, **_kwargs: {
            "status": "committed",
            "automatic_rollback": False,
            "failures": [],
            "restore_commands": [],
        },
    )

    result = installer.resume_managed_upgrade(managed_state_dir=state_dir)
    terminal = installer._read_managed_transaction(state_dir)

    assert result["mode"] == "managed-resume-commit-complete"
    assert result["ok"] is True
    assert terminal["phase"] == "committed"
    assert rollback_calls == 0


def test_resume_commit_cleanup_failure_stays_retryable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = _candidate_target(home, version="2.0.1")
    state_dir = _private_state(home)
    snapshot = _snapshot(home)
    installer._begin_managed_transaction(
        state_dir,
        home=home.resolve(),
        target=target.resolve(),
        snapshot=snapshot,
        previous_plugin_existed=True,
        previous_version="1.10.3",
        target_version="2.0.1",
        requires_vector_degrade=False,
    )
    installer._advance_managed_transaction(
        state_dir,
        "commit_started",
        snapshot=snapshot,
        plugin_backup_path="",
        plugin_replaced=True,
    )
    monkeypatch.setattr(
        installer,
        "committed_activation_receipt",
        lambda *_args, **_kwargs: {
            "status": "commit_cleanup_failed",
            "automatic_rollback": False,
            "failures": ["injected cleanup failure"],
            "restore_commands": [],
        },
    )

    result = installer.resume_managed_upgrade(managed_state_dir=state_dir)
    pending = installer._read_managed_transaction(state_dir)

    assert result["managed_retryable"] is True
    assert result["safe_to_restart_previous"] is False
    assert pending["phase"] == "commit_cleanup_pending"
