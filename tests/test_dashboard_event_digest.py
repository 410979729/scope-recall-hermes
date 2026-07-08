"""Dashboard coverage for event-digest health metrics."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from scope_recall.candidate_extraction import extract_candidates_from_packet
from scope_recall.candidate_store import store_event_candidates
from scope_recall.event_digest import MemoryEvent, build_evidence_packet
from scope_recall.models import RuntimeScope
from scope_recall.sql_store import ensure_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = PLUGIN_ROOT / "scripts" / "report.dashboard.py"


def _load_dashboard_module():
    spec = importlib.util.spec_from_file_location("scope_recall_report_dashboard", DASHBOARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_includes_event_digest_health_summary(tmp_path):
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    packet = build_evidence_packet(
        MemoryEvent(
            kind="task_closeout",
            scope_id="scope-a",
            session_id="session-a",
            turn_number=4,
            content="User prefers concise Chinese release reports with exact verification outputs.",
            metadata={},
        )
    )
    store_event_candidates(
        conn,
        candidates=extract_candidates_from_packet(packet).candidates,
        scope=RuntimeScope(platform="telegram", user_id="user-a", chat_id="chat-a"),
        scope_id="scope-a",
        session_id="session-a",
        dry_run=False,
    )
    conn.close()

    dashboard = _load_dashboard_module()
    payload = dashboard.build_dashboard(PLUGIN_ROOT, tmp_path)

    assert payload["checks"]["event_digest"]["ok"] is True
    assert payload["summary"]["event_digest_status"] == "ready"
    assert payload["summary"]["event_digest_candidates_persisted"] == 1
    assert payload["summary"]["event_digest_audit_events"] == 1
    assert payload["summary"]["event_digest_audit_missing"] == 0
    assert payload["sections"]["event_digest"]["candidates_persisted"] == 1
