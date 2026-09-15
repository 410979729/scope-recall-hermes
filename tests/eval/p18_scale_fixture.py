"""TEST-only 10k/100k cognitive scale fixture.

This module constructs a bounded interference library for M45/J08. It is not
a production Capture path and must not be imported by product code.

Scale fixture writes source_events plus lexical_projection only. It does not
enqueue consolidate/embed work, call external models, run real embeddings, or
preseed claims. Semantic acceptance stays on the existing Core capture and
drain controls.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from scope_recall.core.events import lexical_terms


DATASET_ID = "P18-TEST-ONLY-SCALE-FIXTURE"
EXTRA_JSON = json.dumps(
    {
        "fixture_kind": "scale_interference",
        "not_a_production_capture_shortcut": True,
        "not_production_write_path": True,
        "test_only_bulk_fixture": True,
    },
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
)
INSERT_SOURCE = """INSERT OR IGNORE INTO source_events(
    event_id,source_event_key,source_revision,source_group_key,segment_index,segment_total,
    scope_id,session_id,project_id,branch_id,origin,role,content,content_sha256,event_sha256,
    occurred_at,recorded_at,persisted_at,time_precision,capture_state,source_original_origin,
    dataset_id,import_provenance_sha256,extra_json,capture_gaps_json,read_blocked,suppressed)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
INSERT_LEXICAL = "INSERT OR IGNORE INTO lexical_projection(term,event_id,source_revision) VALUES (?,?,?)"


class ScaleFixtureError(ValueError):
    """Isolated TEST fixture refused to write."""


def _require_test_mode(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT test_mode FROM instance_meta WHERE singleton=1"
    ).fetchone()
    if row is None or int(row[0]) != 1:
        raise ScaleFixtureError("test_mode_required")


def _event_id(series: str, index: int) -> str:
    digest = hashlib.sha256(f"{series}/{index}".encode("utf-8")).hexdigest()
    return "p18fix-" + digest


def install_test_only_scale_fixture(
    *,
    db_path: Path,
    scope_id: str,
    session_id: str,
    project_id: str | None,
    branch_id: str | None,
    series: str,
    start: int,
    maximum: int,
    template: str,
    now: str,
    batch_size: int = 1000,
) -> dict[str, Any]:
    """Install synthetic interference rows without production Capture."""
    if type(start) is not int or type(maximum) is not int or not 1 <= start <= maximum <= 100000:
        raise ScaleFixtureError("scale_limit")
    if type(batch_size) is not int or batch_size < 1:
        raise ScaleFixtureError("batch_size")
    if not isinstance(template, str) or not template.strip():
        raise ScaleFixtureError("generator_template")
    try:
        template.format(index=1, bucket=1, size=1)
    except (KeyError, IndexError, ValueError) as exc:
        raise ScaleFixtureError("generator_template") from exc
    path = Path(db_path)
    started = time.monotonic()
    inserted = 0
    with sqlite3.connect(path, timeout=30) as connection:
        _require_test_mode(connection)
        connection.execute("BEGIN IMMEDIATE")
        for begin in range(start, maximum + 1, batch_size):
            end = min(begin + batch_size - 1, maximum)
            sources = []
            terms = []
            for index in range(begin, end + 1):
                text = template.format(index=index, bucket=(index * 17) % 997, size=1 + (index * 31) % 83)
                ref = _event_id(series, index)
                key = f"{series}/{index}"
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                event_hash = hashlib.sha256(f"{DATASET_ID}:{key}:{digest}".encode("utf-8")).hexdigest()
                sources.append(
                    (
                        ref, key, 1, key, 0, 1, scope_id, session_id, project_id, branch_id,
                        "external_document", "document", text, digest, event_hash, now, now, now,
                        "instant", "complete", None, DATASET_ID, None, EXTRA_JSON, "[]", 0, 0,
                    )
                )
                terms.extend((term, ref, 1) for term in lexical_terms(text))
            before = connection.total_changes
            connection.executemany(INSERT_SOURCE, sources)
            connection.executemany(INSERT_LEXICAL, terms)
            inserted += max(0, connection.total_changes - before)
        prefix = series + "/"
        count = connection.execute(
            "SELECT count(*) FROM source_events WHERE substr(source_event_key,1,?)=?",
            (len(prefix), prefix),
        ).fetchone()[0]
        work = connection.execute(
            "SELECT count(*) FROM work_items w JOIN source_events e "
            "ON e.event_id=w.subject_ref AND e.source_revision=w.subject_revision "
            "WHERE substr(e.source_event_key,1,?)=?",
            (len(prefix), prefix),
        ).fetchone()[0]
        claims = connection.execute("SELECT count(*) FROM claims").fetchone()[0]
        if work:
            raise ScaleFixtureError("scale_fixture_must_not_enqueue_work")
        connection.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        connection.commit()
    return {
        "actual_source_objects": count,
        "claims_preseeded": False,
        "dataset_id": DATASET_ID,
        "elapsed_seconds": time.monotonic() - started,
        "external_model_calls": 0,
        "inserted_or_ignored_change_units": inserted,
        "maximum": maximum,
        "not_production_write_path": True,
        "path": "test_only_bulk_fixture",
        "preseeded_claims": claims,
        "real_embeddings": 0,
        "series_id": series,
        "start_index": start,
        "work_items_for_series": 0,
    }


def series_count(db_path: Path, series: str) -> int:
    prefix = series + "/"
    with sqlite3.connect(Path(db_path), timeout=30) as connection:
        return int(
            connection.execute(
                "SELECT count(*) FROM source_events WHERE substr(source_event_key,1,?)=?",
                (len(prefix), prefix),
            ).fetchone()[0]
        )


def refuse_product_import() -> Mapping[str, Any]:
    return {
        "exported_from_scope_recall_package": False,
        "module": "tests.eval.p18_scale_fixture",
        "production_capture_shortcut": False,
    }
