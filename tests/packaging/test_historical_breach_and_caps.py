"""Exact historical meter_breach exceptions and effective Codex route caps."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402
import model_receipt_evidence as evidence  # noqa: E402
from test_model_receipt_validation import _reference  # noqa: E402


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, payload: dict) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _hermes_ledger(path: Path, *, row_id: int = 4340, reserved_in: int = 100, actual_in: int = 120) -> dict:
    exact = {
        "table": "requests",
        "id": row_id,
        "batch": "P18_EVALUATION",
        "model": "deepseek-v4-flash",
        "reserved_input": reserved_in,
        "reserved_output": 16,
        "actual_input": actual_in,
        "actual_output": 8,
        "status": "meter_breach",
        "charge_micro_usd": 1,
    }
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE requests (
                id INTEGER PRIMARY KEY, batch TEXT, model TEXT,
                reserved_input INTEGER, reserved_output INTEGER,
                actual_input INTEGER, actual_output INTEGER,
                status TEXT, charge_micro_usd INTEGER
            )"""
        )
        db.execute(
            """CREATE TABLE codex_submissions (
                reserved_input INTEGER, reserved_output INTEGER,
                actual_input INTEGER, actual_output INTEGER, status TEXT
            )"""
        )
        db.execute(
            """INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                exact["id"], exact["batch"], exact["model"],
                exact["reserved_input"], exact["reserved_output"],
                exact["actual_input"], exact["actual_output"],
                exact["status"], exact["charge_micro_usd"],
            ),
        )
    return exact


def _codex_ledger(path: Path, *, operation_id: str = "op-1") -> dict:
    exact = {
        "table": "codex_submissions",
        "operation_id": operation_id,
        "reserved_input": 10,
        "reserved_output": 4,
        "actual_input": 12,
        "actual_output": 3,
        "status": "meter_breach",
    }
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE requests (
                reserved_input INTEGER, reserved_output INTEGER,
                actual_input INTEGER, actual_output INTEGER,
                status TEXT, charge_micro_usd INTEGER
            )"""
        )
        db.execute(
            """CREATE TABLE codex_submissions (
                operation_id TEXT, reserved_input INTEGER, reserved_output INTEGER,
                actual_input INTEGER, actual_output INTEGER, status TEXT
            )"""
        )
        db.execute(
            """INSERT INTO codex_submissions VALUES (?,?,?,?,?,?)""",
            (
                exact["operation_id"], exact["reserved_input"], exact["reserved_output"],
                exact["actual_input"], exact["actual_output"], exact["status"],
            ),
        )
    return exact


def test_frozen_hermes_authorization_without_table_key_is_accepted(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.sqlite3"
    exact = _hermes_ledger(ledger)
    payload = {key: value for key, value in exact.items() if key != "table"}
    raw = _write_json(tmp_path / "hermes-auth.json", {"covered_historical_breaches": [payload]})
    monkeypatch.setattr(evidence, "HERMES_HISTORICAL_AUTHORIZATION_SHA256", _sha(raw))
    totals = evidence.ledger_totals(ledger, hermes_historical_authorization=raw)
    assert totals["go_calls"] == 1
    assert totals["go_input_tokens"] == exact["actual_input"]


def test_frozen_hermes_turn_v1_hash_is_pinned():
    path = Path(r"F:\SCOPERECALL更新项目\.execution\budget-authorization-20260908-hermes-turn-v1.json")
    assert path.is_file()
    assert _sha(path.read_bytes()) == evidence.HERMES_HISTORICAL_AUTHORIZATION_SHA256
    assert evidence.HERMES_HISTORICAL_AUTHORIZATION_SHA256 != "0" * 64


def test_exact_authorized_historical_hermes_breach_is_accepted(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.sqlite3"
    exact = _hermes_ledger(ledger)
    raw = _write_json(tmp_path / "hermes-auth.json", {"covered_historical_breaches": [exact]})
    monkeypatch.setattr(evidence, "HERMES_HISTORICAL_AUTHORIZATION_SHA256", _sha(raw))
    totals = evidence.ledger_totals(ledger, hermes_historical_authorization=raw)
    assert totals["go_calls"] == 1
    assert totals["go_input_tokens"] == exact["actual_input"]


def test_different_hermes_breach_is_rejected(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.sqlite3"
    exact = _hermes_ledger(ledger, row_id=4340)
    other = dict(exact, id=9999)
    raw = _write_json(tmp_path / "hermes-auth.json", {"covered_historical_breaches": [other]})
    monkeypatch.setattr(evidence, "HERMES_HISTORICAL_AUTHORIZATION_SHA256", _sha(raw))
    with pytest.raises(ValueError, match="breached"):
        evidence.ledger_totals(ledger, hermes_historical_authorization=raw)


def test_exact_authorized_historical_codex_breach_is_accepted(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.sqlite3"
    exact = _codex_ledger(ledger)
    raw = _write_json(tmp_path / "codex-auth.json", {"covered_historical_breaches": [exact]})
    monkeypatch.setattr(evidence, "ATTEMPT_AUTHORIZATION_SHA256", _sha(raw))
    totals = evidence.ledger_totals(ledger, codex_attempt_authorization=raw)
    assert totals["codex_calls"] == 1
    assert totals["codex_input_tokens"] == exact["actual_input"]


def test_altered_historical_row_is_rejected(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.sqlite3"
    exact = _hermes_ledger(ledger, actual_in=120)
    raw = _write_json(tmp_path / "hermes-auth.json", {"covered_historical_breaches": [exact]})
    monkeypatch.setattr(evidence, "HERMES_HISTORICAL_AUTHORIZATION_SHA256", _sha(raw))
    with sqlite3.connect(ledger) as db:
        db.execute("UPDATE requests SET actual_input=121 WHERE id=4340")
    with pytest.raises(ValueError, match="breached"):
        evidence.ledger_totals(ledger, hermes_historical_authorization=raw)


def test_effective_5000_call_cap_is_accepted(tmp_path, monkeypatch):
    auth = {
        "batch": "P18_EVALUATION",
        "call_cap": 5000,
        "input_cap": 400_000_000,
        "output_cap": 10_000_000,
    }
    raw = _write_json(tmp_path / "attempt.json", auth)
    monkeypatch.setattr(evidence, "ATTEMPT_AUTHORIZATION_SHA256", _sha(raw))
    budget = {"codex_attempt_authorization": _reference(tmp_path / "attempt.json")}
    caps = evidence.effective_budget_caps(budget, tmp_path)
    assert caps["codex_calls"] == 5000
    assert caps["codex_input_tokens"] == 400_000_000
    assert caps["codex_output_tokens"] == 10_000_000
    source = Path(check.__file__).read_text(encoding="utf-8")
    assert 'caps.get("codex_calls") != P18_CODEX_CALL_CAP' not in source
    assert 'caps.get("codex_calls") != codex_cap' in source
    assert "by_host[METHOD_ID] > codex_cap" in source


def test_over_5000_codex_host_calls_are_rejected():
    source = Path(check.__file__).read_text(encoding="utf-8")
    assert "codex_cap = effective_caps[\"codex_calls\"]" in source
    assert "by_host[METHOD_ID] > P18_CODEX_CALL_CAP" not in source
    assert "by_host[METHOD_ID] > codex_cap" in source


def test_missing_authorization_override_keeps_original_codex_cap(tmp_path):
    caps = evidence.effective_budget_caps({}, tmp_path)
    assert caps["codex_calls"] == 1500
    assert caps["codex_input_tokens"] == 40_000_000
    assert caps["codex_output_tokens"] == 2_000_000
