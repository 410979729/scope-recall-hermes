"""Operator workflow for building and reviewing the CJK lexical shadow index.

This module composes the lexical-generation state machine with real retrieval
views.  It commits each bounded backfill page so an interrupted maintenance run
can resume from its durable rowid watermark.  Quality receipts contain counts and
statuses only; sampled memory text and queries are never persisted.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .capture_filters import sanitize_report_text
from .lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_QUALITY_PROVENANCE,
    LEXICAL_SHADOW_TABLE,
    backfill_generation,
    create_shadow_generation,
    current_generation_id,
    generation_integrity_report,
    generation_status,
    lexical_source_binding,
    lexical_quality_evidence_fingerprint,
    lexical_schema_status,
    mark_generation_ready,
)
from .lifecycle_policy import ordinary_recall_lifecycle_visible_sql
from .sql_store import ensure_schema, store_row
from .storage_views import search_db_memories

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{3,}")
_ENGLISH_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")


class _QualityProvider:
    """Minimal provider adapter for exercising the production SQLite read view."""

    def __init__(self, conn: sqlite3.Connection, scope_ids: list[str]):
        self._conn = conn
        self._lock = threading.RLock()
        self._accessible_scope_ids = list(scope_ids)
        self._retrieval_config = {"candidate_pool": 20, "min_score": 0.18}

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    @staticmethod
    def _config_value(_key: str, default: Any) -> Any:
        return default


def _search_ids(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    query: str,
    generation_override: str,
    allow_unreviewed: bool,
) -> set[str]:
    provider = _QualityProvider(conn, [scope_id])
    return {
        str(item.id)
        for item in search_db_memories(
            provider,
            query,
            limit=10,
            generation_override=generation_override,
            allow_unreviewed_generation=allow_unreviewed,
        )
    }


def _store_synthetic(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    timestamp: str,
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="synthetic-scope",
        platform="local",
        user_id="quality-gate",
        chat_id="quality-gate",
        thread_id="",
        gateway_session_key="",
        agent_identity="scope-recall-quality",
        agent_workspace="synthetic",
        session_id="synthetic",
        source="quality-fixture",
        target="memory",
        content=content,
        metadata=json.dumps({"lifecycle": "promoted"}),
        commit=False,
        timestamp=timestamp,
        enqueue_vector_intent=False,
    )


def _synthetic_quality_report() -> dict[str, int]:
    """Exercise the historical 1-old-target/40-new-noise CJK failure in memory."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        _store_synthetic(
            conn,
            "target",
            "生产数据库迁移方案：先做全量备份，校验副本，安排切换窗口，并在变更前完成回滚演练。",
            "2020-01-01T00:00:00+00:00",
        )
        _store_synthetic(
            conn,
            "english-target",
            "OAuth redirect validation preserves exact same-origin transport safety.",
            "2021-01-01T00:00:00+00:00",
        )
        for index in range(40):
            _store_synthetic(
                conn,
                f"noise-{index:02d}",
                f"数据库监控日报第{index:02d}期：检查数据库容量、连接数和告警状态。",
                f"2026-08-05T12:{index:02d}:00+00:00",
            )
        conn.commit()
        create_shadow_generation(conn)
        while True:
            batch = backfill_generation(
                conn,
                LEXICAL_GENERATION_ID,
                batch_size=7,
            )
            conn.commit()
            if bool(batch["complete"]):
                break
        queries = (
            "数据库迁移方案",
            "生产库切换前需要做什么",
            "上线前怎么做回滚演练",
        )
        found = 0
        for query in queries:
            ids = _search_ids(
                conn,
                scope_id="synthetic-scope",
                query=query,
                generation_override=LEXICAL_GENERATION_ID,
                allow_unreviewed=True,
            )
            found += int("target" in ids)
        legacy_english = _search_ids(
            conn,
            scope_id="synthetic-scope",
            query="OAuth redirect validation",
            generation_override="",
            allow_unreviewed=False,
        )
        shadow_english = _search_ids(
            conn,
            scope_id="synthetic-scope",
            query="OAuth redirect validation",
            generation_override=LEXICAL_GENERATION_ID,
            allow_unreviewed=True,
        )
        return {
            "cjk_queries": len(queries),
            "cjk_expected_found": found,
            "english_regressions": len(legacy_english - shadow_english),
        }
    finally:
        conn.close()


def _cjk_query(content: str, summary: str) -> str:
    runs = _CJK_RUN.findall(f"{content}\n{summary}")
    if not runs:
        return ""
    longest = max(runs, key=len)
    return longest[:12]


def _english_query(content: str, summary: str) -> str:
    words = _ENGLISH_WORD.findall(f"{content} {summary}")
    unique: list[str] = []
    seen: set[str] = set()
    for word in words:
        normalized = word.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(word)
        if len(unique) >= 3:
            break
    return " ".join(unique)


def _live_quality_report(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    sample_limit: int,
) -> dict[str, int]:
    visible = ordinary_recall_lifecycle_visible_sql("m")
    rows = conn.execute(
        f"""
        SELECT m.id, m.scope_id, m.content, m.summary
        FROM memories m
        WHERE {visible}
        ORDER BY m.updated_at DESC, m.id ASC
        LIMIT ?
        """,
        (max(64, sample_limit * 20),),
    ).fetchall()
    cjk_cases: list[tuple[str, str, str]] = []
    english_cases: list[tuple[str, str, str]] = []
    seen_cjk_queries: set[tuple[str, str]] = set()
    seen_english_queries: set[tuple[str, str]] = set()
    for row in rows:
        row_id = str(row["id"])
        scope_id = str(row["scope_id"])
        content = str(row["content"])
        summary = str(row["summary"])
        if len(cjk_cases) < sample_limit:
            query = _cjk_query(content, summary)
            query_key = (scope_id, query)
            if query and query_key not in seen_cjk_queries:
                seen_cjk_queries.add(query_key)
                cjk_cases.append((row_id, scope_id, query))
        if len(english_cases) < sample_limit:
            query = _english_query(content, summary)
            query_key = (scope_id, query)
            if query and query_key not in seen_english_queries:
                seen_english_queries.add(query_key)
                english_cases.append((row_id, scope_id, query))
        if len(cjk_cases) >= sample_limit and len(english_cases) >= sample_limit:
            break

    cjk_found = 0
    for expected_id, scope_id, query in cjk_cases:
        ids = _search_ids(
            conn,
            scope_id=scope_id,
            query=query,
            generation_override=generation_id,
            allow_unreviewed=True,
        )
        cjk_found += int(expected_id in ids)

    english_regressions = 0
    for _expected_id, scope_id, query in english_cases:
        legacy = _search_ids(
            conn,
            scope_id=scope_id,
            query=query,
            generation_override="",
            allow_unreviewed=False,
        )
        supplemental = _search_ids(
            conn,
            scope_id=scope_id,
            query=query,
            generation_override=generation_id,
            allow_unreviewed=True,
        )
        english_regressions += len(legacy - supplemental)
    return {
        "cjk_queries": len(cjk_cases),
        "cjk_expected_found": cjk_found,
        "english_queries": len(english_cases),
        "english_regressions": english_regressions,
    }


def validate_lexical_generation(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
    *,
    sample_limit: int = 24,
) -> dict[str, Any]:
    """Run integrity, fixed CJK, and bounded live dual-read quality gates."""

    if sample_limit < 1 or sample_limit > 128:
        raise ValueError("sample_limit must be between 1 and 128")
    integrity = generation_integrity_report(conn, generation_id)
    synthetic = _synthetic_quality_report()
    live = _live_quality_report(
        conn,
        generation_id,
        sample_limit=sample_limit,
    )
    cjk_queries = int(synthetic["cjk_queries"]) + int(live["cjk_queries"])
    cjk_found = int(synthetic["cjk_expected_found"]) + int(
        live["cjk_expected_found"]
    )
    english_regressions = int(synthetic["english_regressions"]) + int(
        live["english_regressions"]
    )
    ok = (
        bool(integrity.get("healthy"))
        and cjk_found == cjk_queries
        and english_regressions == 0
    )
    receipt = {
        "ok": ok,
        "status": "ready" if ok else "blocked",
        "generation_id": generation_id,
        "synthetic_cjk_queries": int(synthetic["cjk_queries"]),
        "synthetic_cjk_expected_found": int(synthetic["cjk_expected_found"]),
        "live_cjk_queries": int(live["cjk_queries"]),
        "live_cjk_expected_found": int(live["cjk_expected_found"]),
        "english_queries": int(live["english_queries"]) + 1,
        "english_regressions": english_regressions,
        "cjk_queries": cjk_queries,
        "cjk_expected_found": cjk_found,
        "integrity": integrity,
        "source_binding": lexical_source_binding(conn),
        "provenance": dict(LEXICAL_QUALITY_PROVENANCE),
        "contains_raw_samples": False,
    }
    receipt["evidence_fingerprint"] = lexical_quality_evidence_fingerprint(receipt)
    return receipt


def plan_lexical_migration(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
) -> dict[str, Any]:
    """Return a strictly read-only lexical migration plan/status report."""

    schema = lexical_schema_status(conn)
    if not bool(schema.get("current")):
        return {
            "ok": True,
            "status": "schema_missing",
            "dry_run": True,
            "generation_id": generation_id,
            "current_generation_id": current_generation_id(conn),
            "schema": schema,
            "writes": [],
        }
    manifest = generation_status(conn, generation_id)
    integrity = (
        generation_integrity_report(conn, generation_id)
        if str(manifest.get("status") or "") not in {"absent", "schema_missing"}
        else {}
    )
    return {
        "ok": True,
        "status": str(manifest.get("status") or "absent"),
        "dry_run": True,
        "generation_id": generation_id,
        "current_generation_id": current_generation_id(conn),
        "schema": schema,
        "manifest": manifest,
        "integrity": integrity,
        "writes": [],
    }


def build_lexical_generation(
    conn: sqlite3.Connection,
    generation_id: str = LEXICAL_GENERATION_ID,
    *,
    batch_size: int = 500,
    sample_limit: int = 24,
) -> dict[str, Any]:
    """Create/resume, batch-commit, validate, and mark one generation READY."""

    try:
        manifest = create_shadow_generation(conn, generation_id)
        resumed_from = int(manifest.get("last_backfilled_rowid") or 0)
        conn.commit()
        if str(manifest.get("status") or "") == "active":
            quality = validate_lexical_generation(
                conn,
                generation_id,
                sample_limit=sample_limit,
            )
            return {
                "ok": bool(quality.get("ok")),
                "status": "active",
                "generation_id": generation_id,
                "resumed_from_rowid": resumed_from,
                "batches": 0,
                "processed": 0,
                "quality": quality,
            }
        batches = 0
        processed = 0
        while True:
            batch = backfill_generation(
                conn,
                generation_id,
                batch_size=batch_size,
            )
            conn.commit()
            batches += 1
            processed += int(batch.get("processed") or 0)
            if bool(batch.get("complete")):
                break
        quality = validate_lexical_generation(
            conn,
            generation_id,
            sample_limit=sample_limit,
        )
        if not bool(quality.get("ok")):
            raise RuntimeError("lexical generation quality gate failed")
        ready = mark_generation_ready(
            conn,
            generation_id,
            quality_receipt=quality,
        )
        conn.commit()
        return {
            "ok": True,
            "status": "ready",
            "generation_id": generation_id,
            "resumed_from_rowid": resumed_from,
            "batches": batches,
            "processed": processed,
            "quality": quality,
            "manifest": ready,
            "shadow_table": LEXICAL_SHADOW_TABLE,
        }
    except Exception as exc:
        conn.rollback()
        if lexical_schema_status(conn).get("current"):
            conn.execute(
                """
                UPDATE lexical_generations
                SET status='failed', quality_ok=0, updated_at=?, error=?
                WHERE generation_id=? AND status='building'
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    sanitize_report_text(str(exc))[:500],
                    generation_id,
                ),
            )
            conn.commit()
        raise
