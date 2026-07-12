"""Read-only adversarial probes for Scope Recall release boundaries.

These tests intentionally describe the desired fail-closed contracts. They use
only in-memory SQLite and temporary directories; no live store is mutated.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scope_recall.config import load_runtime_config, save_runtime_config
from scope_recall.freshness import fact_freshness_report
from scope_recall.journal_store import append_journal_entry
from scope_recall.memory_browser import explain_recall, list_memories
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema, now_iso, store_row, update_row
from scope_recall.storage_views import _ACTIVE_MEMORY_SQL


def _insert_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    lifecycle: str = "",
    memory_type: str = "factual",
    target: str = "ops",
) -> None:
    now = now_iso()
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, dedup_key, metadata
        ) VALUES (?, 'scope', 'telegram', 'joy', '', '', '', 'yuheng', 'hermes', 's',
                  'tool-store', ?, 'audit fact', 'audit fact', ?, ?, 0, ?, ?)
        """,
        (
            memory_id,
            target,
            now,
            now,
            memory_id,
            json.dumps({"memory_type": memory_type, "lifecycle": lifecycle}),
        ),
    )


def _insert_freshness(conn: sqlite3.Connection, memory_id: str, freshness_id: str) -> None:
    now = now_iso()
    conn.execute(
        """
        INSERT INTO fact_freshness(
            id, subject_type, subject_id, fact_key, truth_type, validator_kind,
            ttl_days, last_checked_at, valid_until, status, stale_reason, created_at, updated_at
        ) VALUES (?, 'memory', ?, 'endpoint', 'config', 'manual', 7, ?, ?, 'current', '', ?, ?)
        """,
        (freshness_id, memory_id, now, now, now, now),
    )


def test_zero_freshness_coverage_cannot_be_ready() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert_memory(conn, "active-untracked")
    conn.commit()
    report = fact_freshness_report(conn)
    assert report["coverage"]["coverage_percent"] == 0.0
    assert report["status"] != "ready"


def test_hidden_freshness_rows_cannot_inflate_active_coverage() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert_memory(conn, "active-untracked")
    _insert_memory(conn, "archived-tracked", lifecycle="archived")
    _insert_freshness(conn, "archived-tracked", "fresh-archived")
    conn.commit()
    report = fact_freshness_report(conn)
    assert report["coverage"] == {
        "factual_memories": 1,
        "tracked_memory_facts": 0,
        "coverage_percent": 0.0,
    }
    assert report["status"] != "ready"


def test_browser_explain_uses_same_scratch_lifecycle_policy_as_real_recall() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert_memory(conn, "ops-scratch", lifecycle="scratch", target="ops")
    _insert_memory(conn, "general-scratch", lifecycle="scratch", target="general")
    conn.commit()
    actual_ids = {
        str(row[0])
        for row in conn.execute(
            f"SELECT id FROM memories WHERE scope_id = 'scope' AND {_ACTIVE_MEMORY_SQL} ORDER BY id"
        )
    }
    preview = explain_recall(conn, query="audit", scope_id="scope", limit=10)
    preview_ids = {str(item["id"]) for item in preview["results"]}
    assert actual_ids == {"general-scratch"}
    assert preview_ids == actual_ids


def test_line_wrapped_data_url_payload_never_persists() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    scope = RuntimeScope(
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    second_line = "B" * 512
    for turn_number, punctuation in enumerate((".", ")", "]", ",", ";"), start=1):
        raw = (
            "Please preserve this instruction: verify deployment status before.\n"
            "data:image/png;base64,"
            + ("A" * 512)
            + "\n"
            + second_line
            + punctuation
            + " Then report only confirmed facts after."
        )
        append_journal_entry(
            conn,
            scope=scope,
            scope_id=build_scope_id(scope),
            shared_scope_id=build_shared_scope_id(scope),
            session_id="audit",
            turn_number=turn_number,
            role="user",
            content=raw,
        )
    rows = conn.execute("SELECT content FROM journal_entries ORDER BY id").fetchall()
    assert len(rows) == 5
    for row, punctuation in zip(rows, (".", ")", "]", ",", ";"), strict=True):
        persisted = str(row["content"] or "")
        assert "data:image/png;base64" not in persisted
        assert "A" * 128 not in persisted
        assert second_line not in persisted
        assert "verify deployment status before" in persisted
        assert f"{punctuation} Then report only confirmed facts after" in persisted


def test_memory_store_and_update_sink_scrub_folded_data_urls() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    first_payload = "A" * 512
    continuation = "B" * 512
    raw = (
        "before durable instruction data:image/png;base64,"
        + first_payload
        + "\n"
        + continuation
        + ") after durable instruction"
    )
    stored_id, _summary, _updated_at, inserted = store_row(
        conn,
        memory_id="data-url-sink",
        scope_id="scope",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="audit",
        source="tool-store",
        target="ops",
        content=raw,
        allow_duplicate=True,
    )
    assert inserted is True
    persisted = str(conn.execute("SELECT content FROM memories WHERE id = ?", (stored_id,)).fetchone()[0])
    assert "before durable instruction" in persisted
    assert ") after durable instruction" in persisted
    assert "data:image" not in persisted
    assert first_payload[:128] not in persisted
    assert continuation not in persisted

    updated, _summary, _updated_at = update_row(
        conn,
        memory_id=stored_id,
        content=(
            'updated prose {"image":"data:image/jpeg;base64,'
            + first_payload
            + "\n"
            + continuation
            + '"} remains useful'
        ),
        target="ops",
    )
    assert updated is True
    persisted = str(conn.execute("SELECT content FROM memories WHERE id = ?", (stored_id,)).fetchone()[0])
    assert "updated prose" in persisted and "remains useful" in persisted
    assert "data:image" not in persisted
    assert first_payload[:128] not in persisted
    assert continuation not in persisted


def test_update_row_preserves_hidden_lifecycle_and_does_not_rebuild_fts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    stored_id, _summary, _updated_at, inserted = store_row(
        conn,
        memory_id="archived-update",
        scope_id="scope",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="audit",
        source="tool-store",
        target="memory",
        content="Project Atlas archived operational guidance remains durable.",
        allow_duplicate=True,
    )
    assert inserted is True
    metadata = json.loads(str(conn.execute("SELECT metadata FROM memories WHERE id = ?", (stored_id,)).fetchone()[0]))
    metadata["lifecycle"] = "archived"
    metadata["archived_by"] = "test"
    conn.execute(
        "UPDATE memories SET metadata = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), stored_id),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memory_id = ?", (stored_id,)).fetchone()[0] == 1

    updated, _summary, _updated_at = update_row(
        conn,
        memory_id=stored_id,
        content="Project Atlas archived guidance was edited but must remain hidden.",
        target="memory",
    )

    assert updated is True
    persisted = json.loads(str(conn.execute("SELECT metadata FROM memories WHERE id = ?", (stored_id,)).fetchone()[0]))
    assert persisted["lifecycle"] == "archived"
    assert persisted["archived_by"] == "test"
    assert conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memory_id = ?", (stored_id,)).fetchone()[0] == 0
    conn.close()


def test_default_browser_redacts_secret_like_metadata_keys() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = now_iso()
    secret_marker = "audit-secret-value-1234567890"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, dedup_key, metadata
        ) VALUES ('metadata-key', 'scope', 'telegram', 'joy', '', '', '', 'yuheng', 'hermes', 's',
                  'tool-store', 'ops', 'safe content', 'safe content', ?, ?, 0, 'metadata-key', ?)
        """,
        (now, now, json.dumps({f"api_key={secret_marker}": True})),
    )
    conn.commit()
    payload = list_memories(conn, limit=1, raw=False)
    rendered = json.dumps(payload, ensure_ascii=False)
    assert secret_marker not in rendered
    assert payload["memories"][0]["redacted"] is True


def test_save_config_cannot_silently_persist_values_that_reload_rejects(tmp_path: Path) -> None:
    invalid = {
        "journal.max_entries_per_digest": "999",
        "journal.max_entries_per_digets": 999,
    }
    try:
        save_runtime_config(invalid, str(tmp_path))
    except (TypeError, ValueError):
        return
    persisted = json.loads((tmp_path / "scope-recall" / "config.json").read_text(encoding="utf-8"))
    assert persisted["journal"].get("max_entries_per_digest") != "999"
    assert "max_entries_per_digets" not in persisted["journal"]
    reloaded = load_runtime_config(Path(__import__("scope_recall.config", fromlist=["x"]).__file__).resolve().parent, tmp_path / "scope-recall")
    assert reloaded["journal"]["max_entries_per_digest"] != 500 or not persisted
