"""Shadow lexical-generation state machine and trigger contract tests."""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

from scope_recall import lexical_generation
from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_POSTINGS_TABLE,
    LEXICAL_QUALITY_PROVENANCE,
    LEXICAL_SHADOW_TABLE,
    LexicalGenerationError,
    activate_generation,
    backfill_generation,
    create_shadow_generation,
    current_generation_id,
    ensure_lexical_generation_schema,
    generation_integrity_report,
    generation_status,
    lexical_quality_evidence_fingerprint,
    lexical_source_binding,
    lexical_schema_status,
    mark_generation_ready,
    rollback_generation,
)
from scope_recall.sql_store import ensure_schema, store_row


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)
    conn.commit()
    return conn


def _store(
    conn: sqlite3.Connection,
    row_id: str,
    content: str,
    *,
    lifecycle: str = "promoted",
    target: str = "memory",
) -> None:
    store_row(
        conn,
        memory_id=row_id,
        scope_id="scope-a",
        platform="telegram",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="aria",
        agent_workspace="workspace-a",
        session_id="session-a",
        source="user",
        target=target,
        content=content,
        metadata=json.dumps({"lifecycle": lifecycle}),
        commit=False,
        enqueue_vector_intent=False,
    )


def _quality_receipt(conn: sqlite3.Connection) -> dict[str, object]:
    integrity = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    receipt = {
        "ok": True,
        "status": "ready",
        "generation_id": LEXICAL_GENERATION_ID,
        "synthetic_cjk_queries": 3,
        "synthetic_cjk_expected_found": 3,
        "live_cjk_queries": 0,
        "live_cjk_expected_found": 0,
        "english_queries": 1,
        "cjk_queries": 3,
        "cjk_expected_found": 3,
        "english_regressions": 0,
        "integrity": integrity,
        "source_binding": lexical_source_binding(conn),
        "provenance": dict(LEXICAL_QUALITY_PROVENANCE),
        "contains_raw_samples": False,
    }
    receipt["evidence_fingerprint"] = lexical_quality_evidence_fingerprint(receipt)
    return receipt


def test_schema_metadata_is_additive_and_does_not_auto_create_shadow_storage():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    ensure_lexical_generation_schema(conn)

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    status = lexical_schema_status(conn)

    assert status["current"] is True
    assert {"lexical_generations", "lexical_generation_state"} <= tables
    assert LEXICAL_SHADOW_TABLE not in tables
    assert current_generation_id(conn) == ""


def test_shadow_creation_is_explicit_and_backfill_is_bounded_and_resumable():
    conn = _conn()
    for index in range(5):
        _store(conn, f"visible-{index}", f"数据库迁移记录 {index}")
    _store(conn, "hidden", "隐藏数据库迁移记录", lifecycle="candidate")
    conn.commit()

    created = create_shadow_generation(conn)
    first = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()

    assert created["status"] == "building"
    assert first["processed"] == 2
    assert first["complete"] is False
    assert generation_status(conn, LEXICAL_GENERATION_ID)["last_backfilled_rowid"] > 0

    # A concurrent write after the build watermark is maintained by the trigger
    # and must survive resumed backfill without duplicate rows.
    _store(conn, "late", "数据库切换窗口新增记录")
    conn.commit()
    while not bool(backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)["complete"]):
        conn.commit()
    conn.commit()

    report = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    indexed_ids = {
        str(row[0])
        for row in conn.execute(
            f"SELECT memory_id FROM {LEXICAL_SHADOW_TABLE}"
        ).fetchall()
    }
    assert report["healthy"] is True
    assert report["expected_rows"] == 6
    assert report["indexed_rows"] == 6
    assert indexed_ids == {*(f"visible-{index}" for index in range(5)), "late"}


def test_shadow_triggers_track_updates_lifecycle_visibility_and_deletes():
    conn = _conn()
    create_shadow_generation(conn)
    _store(conn, "memory-1", "数据库迁移原稿")
    conn.commit()

    initial = conn.execute(
        f"SELECT content FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone()
    assert initial is not None and initial[0] == "数据库迁移原稿"

    conn.execute(
        "UPDATE memories SET content=?, summary=? WHERE id=?",
        ("数据库迁移修订稿", "数据库迁移修订稿", "memory-1"),
    )
    updated = conn.execute(
        f"SELECT content FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchall()
    assert [row[0] for row in updated] == ["数据库迁移修订稿"]

    conn.execute(
        "UPDATE memories SET metadata=? WHERE id=?",
        (json.dumps({"lifecycle": "archived"}), "memory-1"),
    )
    assert conn.execute(
        f"SELECT 1 FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone() is None

    conn.execute(
        "UPDATE memories SET target=?, metadata=? WHERE id=?",
        ("general", json.dumps({"lifecycle": "scratch"}), "memory-1"),
    )
    assert conn.execute(
        f"SELECT 1 FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone() is not None

    conn.execute("DELETE FROM memories WHERE id='memory-1'")
    assert conn.execute(
        f"SELECT 1 FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    ).fetchone() is None


def test_ready_gate_rejects_integrity_drift_and_accepts_reviewed_quality_receipt():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10)
    conn.execute(
        f"DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-1'"
    )

    with pytest.raises(LexicalGenerationError, match="integrity"):
        mark_generation_ready(
            conn,
            LEXICAL_GENERATION_ID,
            quality_receipt=_quality_receipt(conn),
        )

    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10, reconcile=True)
    ready = mark_generation_ready(
        conn,
        LEXICAL_GENERATION_ID,
        quality_receipt=_quality_receipt(conn),
    )

    assert ready["status"] == "ready"
    assert generation_status(conn, LEXICAL_GENERATION_ID)["quality_ok"] is True


def test_activation_and_rollback_use_cas_and_retain_legacy_storage():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10)
    mark_generation_ready(
        conn,
        LEXICAL_GENERATION_ID,
        quality_receipt=_quality_receipt(conn),
    )

    with pytest.raises(LexicalGenerationError, match="CAS"):
        activate_generation(
            conn,
            LEXICAL_GENERATION_ID,
            expected_current="unexpected",
        )

    activated = activate_generation(
        conn,
        LEXICAL_GENERATION_ID,
        expected_current="",
    )
    assert activated["status"] == "active"
    assert current_generation_id(conn) == LEXICAL_GENERATION_ID

    with pytest.raises(LexicalGenerationError, match="CAS"):
        rollback_generation(conn, expected_current="unexpected")

    rolled_back = rollback_generation(
        conn,
        expected_current=LEXICAL_GENERATION_ID,
    )
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert rolled_back["status"] == "legacy"
    assert current_generation_id(conn) == ""
    assert {"memories_fts", LEXICAL_SHADOW_TABLE} <= tables


def test_ready_rejects_zero_query_unknown_secret_like_quality_receipt():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10)

    forged = {
        "ok": True,
        "cjk_queries": 0,
        "cjk_expected_found": 0,
        "english_regressions": 0,
        "sample": "api_key=must-never-persist",
    }
    with pytest.raises(LexicalGenerationError, match="quality"):
        mark_generation_ready(
            conn,
            LEXICAL_GENERATION_ID,
            quality_receipt=forged,
        )

    manifest = generation_status(conn, LEXICAL_GENERATION_ID)
    assert manifest["status"] == "building"
    assert "api_key" not in str(manifest.get("quality_json") or "")


def test_ready_rejects_tampered_quality_evidence_fingerprint():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10)
    receipt = _quality_receipt(conn)
    receipt["english_queries"] = 2

    with pytest.raises(LexicalGenerationError, match="fingerprint"):
        mark_generation_ready(
            conn,
            LEXICAL_GENERATION_ID,
            quality_receipt=receipt,
        )


def test_activation_rejects_quality_receipt_after_truth_source_changes():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=10)
    mark_generation_ready(
        conn,
        LEXICAL_GENERATION_ID,
        quality_receipt=_quality_receipt(conn),
    )

    _store(conn, "memory-2", "数据库迁移方案后续变更")
    conn.commit()

    with pytest.raises(LexicalGenerationError, match="source"):
        activate_generation(
            conn,
            LEXICAL_GENERATION_ID,
            expected_current="",
        )


def _finish_backfill(conn: sqlite3.Connection, *, batch_size: int = 2) -> None:
    while not bool(
        backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=batch_size)[
            "complete"
        ]
    ):
        conn.commit()
    conn.commit()


def test_backfill_resume_rebuilds_trigger_prewritten_page_rows():
    conn = _conn()
    for index in range(6):
        _store(conn, f"memory-{index}", f"数据库迁移记录 {index}")
    conn.commit()
    create_shadow_generation(conn)
    first = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()
    assert first["processed"] == 2

    # The maintenance trigger pre-writes a shadow row and postings beyond the
    # build watermark. Resumed backfill covers that rowid in its next page.
    conn.execute(
        "UPDATE memories SET content=?, summary=? WHERE id=?",
        ("数据库迁移记录 2 修订稿", "数据库迁移记录 2 修订稿", "memory-2"),
    )
    conn.commit()

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    resumed = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.set_trace_callback(None)
    conn.commit()

    assert resumed["processed"] == 2
    traced = [sql.strip() for sql in statements]
    shadow_delete = next(
        index
        for index, sql in enumerate(traced)
        if sql.startswith(f"DELETE FROM {LEXICAL_SHADOW_TABLE}")
    )
    shadow_insert = next(
        index
        for index, sql in enumerate(traced)
        if sql.startswith(f"INSERT INTO {LEXICAL_SHADOW_TABLE}")
    )
    postings_delete = next(
        index
        for index, sql in enumerate(traced)
        if sql.startswith(f"DELETE FROM {LEXICAL_POSTINGS_TABLE}")
    )
    postings_insert = next(
        index
        for index, sql in enumerate(traced)
        if sql.startswith(f"INSERT OR IGNORE INTO {LEXICAL_POSTINGS_TABLE}")
    )
    assert shadow_delete < shadow_insert
    assert postings_delete < postings_insert

    _finish_backfill(conn)
    report = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    rebuilt = conn.execute(
        f"SELECT content FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id='memory-2'"
    ).fetchall()
    assert [str(row[0]) for row in rebuilt] == ["数据库迁移记录 2 修订稿"]
    assert report["healthy"] is True
    assert report["expected_rows"] == 6
    assert report["indexed_rows"] == 6


def test_backfill_replayed_page_is_idempotent():
    conn = _conn()
    for index in range(4):
        _store(conn, f"memory-{index}", f"数据库迁移记录 {index}")
    conn.commit()
    create_shadow_generation(conn)
    first = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()
    assert first["processed"] == 2

    before_shadow = conn.execute(
        f"SELECT COUNT(*) FROM {LEXICAL_SHADOW_TABLE}"
    ).fetchone()[0]
    before_postings = conn.execute(
        f"SELECT COUNT(*) FROM {LEXICAL_POSTINGS_TABLE}"
    ).fetchone()[0]

    # Rewind the build watermark, replaying the exact same page over rows that
    # are already indexed. The page must rebuild in place, not collide.
    conn.execute(
        "UPDATE lexical_generations SET last_backfilled_rowid=0 WHERE generation_id=?",
        (LEXICAL_GENERATION_ID,),
    )
    conn.commit()
    replayed = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()

    assert replayed["processed"] == 2
    assert (
        conn.execute(f"SELECT COUNT(*) FROM {LEXICAL_SHADOW_TABLE}").fetchone()[0]
        == before_shadow
    )
    assert (
        conn.execute(f"SELECT COUNT(*) FROM {LEXICAL_POSTINGS_TABLE}").fetchone()[0]
        == before_postings
    )

    _finish_backfill(conn)
    report = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    assert report["healthy"] is True
    assert report["duplicate_rows"] == 0


def test_backfill_page_rolls_back_and_replays_within_caller_transaction():
    conn = _conn()
    for index in range(4):
        _store(conn, f"memory-{index}", f"数据库迁移记录 {index}")
    conn.commit()
    create_shadow_generation(conn)
    first = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()
    assert first["processed"] == 2

    # A crash before commit advances neither shadow rows nor the watermark.
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.rollback()
    manifest = generation_status(conn, LEXICAL_GENERATION_ID)
    assert int(manifest["last_backfilled_rowid"]) == int(
        first["last_backfilled_rowid"]
    )
    assert (
        conn.execute(f"SELECT COUNT(*) FROM {LEXICAL_SHADOW_TABLE}").fetchone()[0]
        == 2
    )

    replayed = backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()
    assert replayed["processed"] == 2
    _finish_backfill(conn)
    assert generation_integrity_report(conn, LEXICAL_GENERATION_ID)["healthy"] is True


_POSTINGS_DOCID_INDEX = "idx_lexical_cjk_postings_v1_docid"


def _index_columns(conn: sqlite3.Connection, index: str) -> list[str]:
    return [
        str(row[2])
        for row in conn.execute(f"PRAGMA index_info({index})").fetchall()
    ]


def test_postings_docid_index_covers_new_and_existing_generations():
    conn = _conn()
    _store(conn, "memory-1", "数据库迁移方案")
    conn.commit()

    create_shadow_generation(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (_POSTINGS_DOCID_INDEX,),
    ).fetchone()
    assert row is not None
    assert _index_columns(conn, _POSTINGS_DOCID_INDEX) == ["docid", "term"]

    # A generation created before the index existed keeps working: resuming it
    # must backfill the missing index instead of requiring a rebuild.
    conn.execute(f"DROP INDEX {_POSTINGS_DOCID_INDEX}")
    conn.commit()
    create_shadow_generation(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (_POSTINGS_DOCID_INDEX,),
    ).fetchone()
    assert row is not None
    assert _index_columns(conn, _POSTINGS_DOCID_INDEX) == ["docid", "term"]


def _indexed_fixture(contents: list[str]) -> sqlite3.Connection:
    conn = _conn()
    for index, content in enumerate(contents):
        _store(conn, f"memory-{index}", content)
    conn.commit()
    create_shadow_generation(conn)
    _finish_backfill(conn)
    return conn


def test_integrity_report_requires_postings_docid_index():
    conn = _indexed_fixture(["数据库迁移方案", "索引重建记录"])

    baseline = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    assert baseline["healthy"] is True
    assert baseline["postings_docid_index_present"] is True

    conn.execute(f"DROP INDEX {_POSTINGS_DOCID_INDEX}")
    conn.commit()
    missing_index = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    assert missing_index["postings_docid_index_present"] is False
    assert missing_index["healthy"] is False

    create_shadow_generation(conn)
    repaired = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    assert repaired["postings_docid_index_present"] is True
    assert repaired["healthy"] is True


def _swap_shadow_identity(
    conn: sqlite3.Connection, first_id: str, second_id: str
) -> None:
    rows = {}
    for memory_id in (first_id, second_id):
        row = conn.execute(
            f"SELECT rowid, content, summary FROM {LEXICAL_SHADOW_TABLE} "
            "WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        assert row is not None
        rows[memory_id] = (int(row[0]), str(row[1]), str(row[2]))
    conn.execute(
        f"DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE memory_id IN (?, ?)",
        (first_id, second_id),
    )
    # Content follows the memory_id while the docid now resolves to the other
    # document: retrieval joins truth on memories.rowid = docid, so a MATCH on
    # one document's terms silently returns the swapped neighbour.
    first_rowid, first_content, first_summary = rows[first_id]
    second_rowid, second_content, second_summary = rows[second_id]
    conn.execute(
        f"INSERT INTO {LEXICAL_SHADOW_TABLE}(rowid, memory_id, content, summary)"
        " VALUES (?, ?, ?, ?)",
        (second_rowid, first_id, first_content, first_summary),
    )
    conn.execute(
        f"INSERT INTO {LEXICAL_SHADOW_TABLE}(rowid, memory_id, content, summary)"
        " VALUES (?, ?, ?, ?)",
        (first_rowid, second_id, second_content, second_summary),
    )
    conn.commit()


def test_integrity_report_fails_closed_on_swapped_rowid_identity():
    conn = _indexed_fixture(["数据库迁移方案甲", "索引重建记录乙"])

    _swap_shadow_identity(conn, "memory-0", "memory-1")
    report = generation_integrity_report(conn, LEXICAL_GENERATION_ID)

    assert report["healthy"] is False
    assert report["identity_mismatch_rows"] == 2
    assert report["content_drift_rows"] == 2
    assert report["missing_rows"] == 0
    assert report["stale_rows"] == 0
    assert report["duplicate_rows"] == 0


def test_integrity_report_identity_mismatch_fails_closed_when_contents_match():
    conn = _indexed_fixture(["数据库迁移方案", "索引重建记录"])
    # Equalize the documents after indexing (triggers keep the shadow in
    # sync), so a later swap is invisible to every content-level counter.
    conn.execute(
        "UPDATE memories SET content=?, summary=? WHERE id IN ('memory-0', 'memory-1')",
        ("相同的数据库文本", "相同的数据库文本"),
    )
    conn.commit()

    baseline = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    assert baseline["healthy"] is True
    assert baseline["identity_mismatch_rows"] == 0

    _swap_shadow_identity(conn, "memory-0", "memory-1")
    report = generation_integrity_report(conn, LEXICAL_GENERATION_ID)

    # Identical documents keep every content-level counter at zero; only the
    # docid/memory_id identity check can see the swap, and it must fail closed.
    assert report["missing_rows"] == 0
    assert report["stale_rows"] == 0
    assert report["hidden_rows"] == 0
    assert report["duplicate_rows"] == 0
    assert report["content_drift_rows"] == 0
    assert report["identity_mismatch_rows"] == 2
    assert report["healthy"] is False


def test_integrity_report_keeps_row_level_detectors():
    contents = ["数据库迁移方案", "索引重建记录", "灰度发布窗口"]

    missing_conn = _indexed_fixture(contents)
    missing_conn.execute(f"DELETE FROM {LEXICAL_SHADOW_TABLE} WHERE rowid=1")
    missing_report = generation_integrity_report(missing_conn, LEXICAL_GENERATION_ID)
    assert missing_report["missing_rows"] == 1
    assert missing_report["healthy"] is False

    stale_conn = _indexed_fixture(contents)
    stale_conn.execute(
        f"INSERT INTO {LEXICAL_SHADOW_TABLE}(rowid, memory_id, content, summary)"
        " VALUES (99999, 'ghost', '孤儿记录', '孤儿记录')"
    )
    stale_report = generation_integrity_report(stale_conn, LEXICAL_GENERATION_ID)
    assert stale_report["stale_rows"] == 1
    assert stale_report["healthy"] is False

    hidden_conn = _indexed_fixture(contents)
    _store(hidden_conn, "hidden-1", "隐藏数据库记录", lifecycle="candidate")
    hidden_conn.commit()
    hidden_rowid = int(
        hidden_conn.execute(
            "SELECT rowid FROM memories WHERE id='hidden-1'"
        ).fetchone()[0]
    )
    hidden_conn.execute(
        f"INSERT INTO {LEXICAL_SHADOW_TABLE}(rowid, memory_id, content, summary)"
        " VALUES (?, 'hidden-1', '隐藏数据库记录', '隐藏数据库记录')",
        (hidden_rowid,),
    )
    hidden_report = generation_integrity_report(hidden_conn, LEXICAL_GENERATION_ID)
    assert hidden_report["hidden_rows"] == 1
    assert hidden_report["healthy"] is False

    duplicate_conn = _indexed_fixture(contents)
    duplicate_conn.execute(
        f"INSERT INTO {LEXICAL_SHADOW_TABLE}(rowid, memory_id, content, summary)"
        " VALUES (99998, 'memory-0', '数据库迁移方案', '数据库迁移方案')"
    )
    duplicate_report = generation_integrity_report(
        duplicate_conn, LEXICAL_GENERATION_ID
    )
    assert duplicate_report["duplicate_rows"] == 1
    assert duplicate_report["healthy"] is False

    drift_conn = _indexed_fixture(contents)
    drift_conn.execute(
        f"UPDATE {LEXICAL_SHADOW_TABLE} SET content='被篡改的内容' WHERE rowid=1"
    )
    drift_report = generation_integrity_report(drift_conn, LEXICAL_GENERATION_ID)
    assert drift_report["content_drift_rows"] == 1
    assert drift_report["healthy"] is False

    postings_conn = _indexed_fixture(contents)
    postings_conn.execute(
        f"INSERT INTO {LEXICAL_POSTINGS_TABLE}(term, docid) VALUES ('孤儿词', 99997)"
    )
    postings_report = generation_integrity_report(
        postings_conn, LEXICAL_GENERATION_ID
    )
    assert postings_report["postings_stale_rows"] == 1
    assert postings_report["healthy"] is False


_PUBLIC_SIGNATURES = {
    "ensure_lexical_generation_schema": (["conn"], {}, set()),
    "lexical_schema_status": (["conn"], {}, set()),
    "current_generation_id": (["conn"], {}, set()),
    "create_shadow_generation": (
        ["conn", "generation_id"],
        {"generation_id": LEXICAL_GENERATION_ID},
        set(),
    ),
    "generation_status": (
        ["conn", "generation_id"],
        {"generation_id": LEXICAL_GENERATION_ID},
        set(),
    ),
    "backfill_generation": (
        ["conn", "generation_id", "batch_size", "reconcile"],
        {"generation_id": LEXICAL_GENERATION_ID, "batch_size": 500, "reconcile": False},
        {"batch_size", "reconcile"},
    ),
    "generation_integrity_report": (
        ["conn", "generation_id"],
        {"generation_id": LEXICAL_GENERATION_ID},
        set(),
    ),
    "lexical_source_binding": (["conn"], {}, set()),
    "lexical_quality_evidence_fingerprint": (["receipt"], {}, set()),
    "mark_generation_ready": (
        ["conn", "generation_id", "quality_receipt"],
        {"generation_id": LEXICAL_GENERATION_ID},
        {"quality_receipt"},
    ),
    "activate_generation": (
        ["conn", "generation_id", "expected_current"],
        {"generation_id": LEXICAL_GENERATION_ID},
        {"expected_current"},
    ),
    "rollback_generation": (["conn", "expected_current"], {}, {"expected_current"}),
    "supplemental_table_for_search": (
        ["conn", "generation_override", "allow_unreviewed_override"],
        {"generation_override": None, "allow_unreviewed_override": False},
        {"allow_unreviewed_override"},
    ),
    "lexical_generation_report": (["conn"], {}, set()),
}


def test_public_generation_interfaces_keep_their_signatures():
    for name, (parameters, defaults, keyword_only) in _PUBLIC_SIGNATURES.items():
        signature = inspect.signature(getattr(lexical_generation, name))
        assert list(signature.parameters) == parameters, name
        actual_defaults = {
            key: parameter.default
            for key, parameter in signature.parameters.items()
            if parameter.default is not inspect.Parameter.empty
        }
        assert actual_defaults == defaults, name
        actual_keyword_only = {
            key
            for key, parameter in signature.parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        }
        assert actual_keyword_only == keyword_only, name
