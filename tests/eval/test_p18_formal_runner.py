from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from p18_formal_runner import FormalRunnerError, _observe_hermes_session, run_public_diagnostic, run_transport_operations


HERE = Path(__file__).resolve().parent
PUBLIC_FIXTURE = HERE / "public_fixture.jsonl"
CODEX_FIXTURE = HERE / "fixtures" / "p18_codex_appserver_fixture.jsonl"


def _hermes_fixture(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "agent_card": {"name": "TEST Hermes", "version": "0.21.0"},
                "response": {
                    "jsonrpc": "2.0",
                    "id": "TEST-message-hermes-A-001",
                    "result": {
                        "id": "TEST-task-001",
                        "contextId": "TEST-context-A",
                        "status": {"state": "COMPLETED", "message": {"parts": [{"text": "TEST diagnostic answer"}]}},
                        "artifacts": [],
                        "usage": {"input_tokens": 2, "output_tokens": 3},
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_hermes_public_diagnostic_runs_each_arm_and_retains_response(tmp_path: Path) -> None:
    exchange = tmp_path / "TEST-hermes-exchange.json"
    _hermes_fixture(exchange)
    result = run_public_diagnostic(
        fixture_path=PUBLIC_FIXTURE,
        output_root=tmp_path / "TEST-hermes-run",
        hermes_exchange_fixture=exchange,
    )
    assert result["status"] == "DIAGNOSTIC_ONLY"
    assert result["formal_pass"] is False
    assert len(result["receipts"]) == 4
    for item in result["receipts"]:
        assert item["transport_receipt"]["status"] == "DIAGNOSTIC"
        evidence_path = Path(item["evidence_path"])
        assert evidence_path.is_file()
        saved = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert saved["evidence_class"] == "diagnostic"
        assert saved["host_response"]["artifact_path"].endswith("response.bin")
    saved_run = json.loads((tmp_path / "TEST-hermes-run" / "run-receipt.json").read_text(encoding="utf-8"))
    assert saved_run["formal_pass"] is False


def test_codex_fixture_is_diagnostic_for_all_arms(tmp_path: Path) -> None:
    result = run_public_diagnostic(
        fixture_path=PUBLIC_FIXTURE,
        output_root=tmp_path / "TEST-codex-run",
        codex_fixture=CODEX_FIXTURE,
    )
    assert result["status"] == "DIAGNOSTIC_ONLY"
    assert len(result["receipts"]) == 4
    assert all(item["transport_receipt"]["formal_evaluation"] is False for item in result["receipts"])


def test_diagnostic_requires_exactly_one_fixture_and_test_root(tmp_path: Path) -> None:
    with pytest.raises(FormalRunnerError, match="exactly_one"):
        run_public_diagnostic(fixture_path=PUBLIC_FIXTURE, output_root=tmp_path / "TEST-empty")
    exchange = tmp_path / "TEST-hermes-exchange.json"
    _hermes_fixture(exchange)
    with pytest.raises(FormalRunnerError, match="output_root"):
        run_public_diagnostic(fixture_path=PUBLIC_FIXTURE, output_root=tmp_path / "production", hermes_exchange_fixture=exchange)


def test_formal_transport_path_fails_closed_without_frozen_config(tmp_path: Path) -> None:
    result = run_transport_operations(
        config_path=tmp_path / "TEST-missing-formal-config.json",
        output_root=tmp_path / "TEST-formal-run",
        transport=object(),
        queries=[{"query": "TEST query"}],
        arm_id="C",
    )
    assert result["status"] == "NOT_READY"
    assert result["formal_pass"] is False


def test_hermes_session_observation_reads_real_json_backed_gateway_schema(tmp_path: Path) -> None:
    root = tmp_path / "TEST-hermes-config"
    root.mkdir()
    db_path = root / "state.db"
    session_key = "agent:main:a2a:dm:TEST-context-A"
    entry = {
        "session_key": session_key,
        "session_id": "TEST-session-actual-001",
        "platform": "a2a",
        "origin": {"chat_id": "TEST-context-A", "chat_type": "dm"},
    }
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE gateway_routing(scope TEXT NOT NULL, session_key TEXT NOT NULL, entry_json TEXT NOT NULL, updated_at REAL NOT NULL)")
        db.execute("INSERT INTO gateway_routing VALUES (?,?,?,?)", ("TEST-scope", session_key, json.dumps(entry), 1.0))
    observed = _observe_hermes_session(
        {"session_db": str(db_path), "context_id": "TEST-context-A", "platform": "a2a"},
        {"context_id": "TEST-context-A", "task_id": "TEST-task-A"},
        config_root=root,
    )
    assert observed["session_id"] == "TEST-session-actual-001"
    assert observed["session_key"] == session_key
    assert observed["platform"] == "a2a"


def test_hermes_session_observation_rejects_context_id_promoted_as_session(tmp_path: Path) -> None:
    root = tmp_path / "TEST-hermes-config"
    root.mkdir()
    db_path = root / "state.db"
    session_key = "agent:main:a2a:dm:TEST-context-A"
    entry = {"session_key": session_key, "session_id": "TEST-context-A", "platform": "a2a", "origin": {"chat_id": "TEST-context-A"}}
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE gateway_routing(scope TEXT NOT NULL, session_key TEXT NOT NULL, entry_json TEXT NOT NULL, updated_at REAL NOT NULL)")
        db.execute("INSERT INTO gateway_routing VALUES (?,?,?,?)", ("TEST-scope", session_key, json.dumps(entry), 1.0))
    with pytest.raises(FormalRunnerError, match="session_not_unique"):
        _observe_hermes_session(
            {"session_db": str(db_path), "context_id": "TEST-context-A"},
            {"context_id": "TEST-context-A"},
            config_root=root,
        )
