"""Public offline regressions for retaining the original P18 ledger.

All databases are temporary synthetic fixtures. No operational ledger is read
or modified by these tests.
"""
from __future__ import annotations

import copy
import gc
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import p18_formal_evidence as evidence
from p18_codex_budget import CodexSubmissionBudget
from p18_formal_runner import CodexLedgerBudgetAdapter
from p18_ledger_reference import (
    LedgerReferenceError, freeze_ledger_binding, ledger_path_reference, resolve_ledger_reference,
)
from test_p18_formal_evidence import _append, _bundle, _json


def _external_bundle(tmp_path, monkeypatch, *, host=None, unknown=False):
    root = tmp_path / "TEST-evidence"
    root.mkdir()
    ledger = tmp_path / "TEST-original-budget.sqlite3"
    path, config, record = _bundle(root, monkeypatch, host=host, unknown=unknown, ledger_path=ledger)
    config["ledger_path"] = ledger_path_reference(ledger, root)
    config["ledger_binding"] = freeze_ledger_binding(ledger)
    record["usage"]["ledger_path"] = ledger_path_reference(ledger, root)
    _json(path, config)
    return root, path, config, record, ledger


@pytest.mark.parametrize("host", [None, "hermes_a2a"])
@pytest.mark.parametrize("unknown", [False, True])
def test_external_original_ledger_preserves_formal_accounting(tmp_path, monkeypatch, host, unknown):
    root, path, config, record, ledger = _external_bundle(tmp_path, monkeypatch, host=host, unknown=unknown)
    before = ledger.read_bytes()
    readiness = evidence.verify_formal_run_config(path)
    assert readiness.formal_execution_allowed, readiness.reasons
    assert Path(readiness.details["ledger_path"]) == ledger
    saved = json.loads(_append(root, path, record).read_text(encoding="utf-8"))
    assert saved["status"] == "COMPLETED"
    assert saved["accounted_usage"]["effective_input_tokens"] == (32768 if unknown else 12)
    assert saved["accounted_usage"]["effective_output_tokens"] == (4096 if unknown else 4)
    assert "semantic_pass" not in saved
    assert ledger.read_bytes() == before
    assert not (root / "ledger.sqlite3").exists()
    assert resolve_ledger_reference(record["usage"]["ledger_path"], root, config["ledger_binding"]) == ledger


def test_external_reference_without_original_binding_is_not_ready(tmp_path, monkeypatch):
    _, path, config, _, _ = _external_bundle(tmp_path, monkeypatch)
    del config["ledger_binding"]
    _json(path, config)
    readiness = evidence.verify_formal_run_config(path)
    assert not readiness.formal_execution_allowed
    assert "ledger_binding_required" in readiness.reasons


def test_usage_cannot_substitute_a_byte_identical_copy(tmp_path, monkeypatch):
    root, path, _, record, ledger = _external_bundle(tmp_path, monkeypatch)
    clone = root / "copied-budget.sqlite3"
    shutil.copyfile(ledger, clone)
    forged = copy.deepcopy(record)
    forged["usage"]["ledger_path"] = clone.name
    with pytest.raises(evidence.EvidenceSchemaError, match="ledger_not_frozen_original"):
        _append(root, path, forged)


def test_replacing_original_file_invalidates_preflight_and_usage(tmp_path, monkeypatch):
    root, path, _, record, ledger = _external_bundle(tmp_path, monkeypatch)
    readiness = evidence.verify_formal_run_config(path)
    replacement = tmp_path / "TEST-replacement.sqlite3"
    shutil.copyfile(ledger, replacement)
    # Release completed fixture connections before replacing a SQLite file
    # on Windows; the identity check itself never holds a database handle.
    gc.collect()
    replacement.replace(ledger)
    current = evidence.verify_formal_run_config(path)
    assert not current.formal_execution_allowed
    assert "ledger_file_identity_changed" in current.reasons
    with pytest.raises(evidence.EvidenceSchemaError, match="ledger_file_identity_changed"):
        evidence._validate_usage(record["usage"], root, record, readiness)


def test_normal_budget_growth_preserves_original_identity(tmp_path, monkeypatch):
    root, path, _, record, ledger = _external_bundle(tmp_path, monkeypatch)
    original_binding = freeze_ledger_binding(ledger)
    budget = CodexSubmissionBudget(ledger)
    budget.reserve("TEST-op-2", b'{"input":"PUBLIC TEST second query"}')
    budget.finish("TEST-op-2", "completed", {"input_tokens": 9, "output_tokens": 2})
    assert freeze_ledger_binding(ledger) == original_binding
    assert evidence.verify_formal_run_config(path).formal_execution_allowed
    assert _append(root, path, record).is_file()
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT COUNT(*) FROM codex_submissions").fetchone()[0] == 2


def test_external_ledger_permission_does_not_allow_external_artifacts(tmp_path, monkeypatch):
    root, path, _, record, _ = _external_bundle(tmp_path, monkeypatch)
    record["host_response"]["artifact_path"] = str(tmp_path / "outside-response.json")
    with pytest.raises(evidence.EvidenceSchemaError, match="artifact_path_must_be_relative"):
        _append(root, path, record)


def test_codex_usage_adapter_retains_original_external_path(tmp_path):
    root = tmp_path / "TEST-evidence"
    root.mkdir()
    ledger = tmp_path / "TEST-budget.sqlite3"
    backend = CodexSubmissionBudget(ledger)
    backend.initialize_schema()
    adapter = CodexLedgerBudgetAdapter(backend, operation_root=root / "operations", config_root=root)
    adapter.begin_operation("TEST-op")
    reservation = adapter.reserve("gpt-5.6-luna", b'{"input":"PUBLIC TEST"}')
    adapter.finish(reservation, "completed", {"input_tokens": 7, "output_tokens": 3})
    usage = adapter.formal_usage()
    assert usage["ledger_path"] == str(ledger)
    assert Path(usage["entries"][0]["request"]["path"]).is_absolute() is False
    assert resolve_ledger_reference(usage["ledger_path"], root, freeze_ledger_binding(ledger)) == ledger


@pytest.mark.parametrize("binding_change", [{"inode": True}, {"device": -1}, {"canonical_path": "relative.db"}])
def test_malformed_original_file_bindings_are_rejected(tmp_path, binding_change):
    ledger = tmp_path / "TEST-budget.sqlite3"
    ledger.write_bytes(b"PUBLIC TEST path fixture")
    binding = freeze_ledger_binding(ledger)
    binding.update(binding_change)
    with pytest.raises(LedgerReferenceError, match="ledger_binding_invalid"):
        resolve_ledger_reference(ledger.name, tmp_path, binding)


def test_relative_path_cannot_escape_config_even_with_binding(tmp_path):
    root = tmp_path / "TEST-evidence"
    root.mkdir()
    ledger = tmp_path / "TEST-budget.sqlite3"
    ledger.write_bytes(b"PUBLIC TEST path fixture")
    with pytest.raises(LedgerReferenceError, match="ledger_relative_path_escapes_config"):
        resolve_ledger_reference("../TEST-budget.sqlite3", root, freeze_ledger_binding(ledger))
