from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from scope_recall.sql_store import ensure_schema, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "doctor.py"


def _doctor_module():
    spec = importlib.util.spec_from_file_location("scope_recall_doctor", DOCTOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conn(hermes_home: Path) -> sqlite3.Connection:
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert(conn: sqlite3.Connection, *, memory_id: str, content: str, metadata: str = "{}") -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared-scope",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="ops",
        content=content,
        metadata=metadata,
        allow_duplicate=True,
    )


def test_doctor_memory_secret_scan_fails_on_active_plaintext_secret(tmp_path):
    conn = _conn(tmp_path)
    fake_secret = "sk-" + "A" * 24
    _insert(conn, memory_id="secret-row", content="temporary " + "api_key=" + fake_secret + " should not be durable")
    _insert(conn, memory_id="safe-row", content="Passwordless SSH is configured via key reference, no plaintext secret stored.")
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.memory_secret_report(tmp_path)

    assert payload["active_secret_like_count"] == 1
    assert payload["samples"][0]["id"] == "secret-row"
    assert fake_secret not in payload["samples"][0]["preview"]
    assert "[REDACTED_SECRET]" in payload["samples"][0]["preview"]
    assert check["ok"] is False
    assert recommendations


def test_doctor_memory_secret_scan_ignores_archived_secret_rows(tmp_path):
    conn = _conn(tmp_path)
    fake_secret = "sk-" + "B" * 24
    _insert(
        conn,
        memory_id="archived-secret",
        content="temporary " + "api_key=" + fake_secret + " should not be active",
    )
    conn.execute(
        "UPDATE memories SET metadata = json_set(metadata, '$.lifecycle', 'archived') WHERE id = ?",
        ("archived-secret",),
    )
    conn.commit()
    conn.close()
    doctor = _doctor_module()

    payload, check, recommendations = doctor.memory_secret_report(tmp_path)

    assert payload["active_secret_like_count"] == 0
    assert payload["samples"] == []
    assert check["ok"] is True
    assert recommendations == []
