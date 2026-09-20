"""Tool-output vectors expire after the retention window; the rest of the memory stays.

On a busy instance four in five captured sources were tool output, each
carrying a 12 KB vector -- the bulk of daily growth -- while a few hundred of
170,000 sources ever became claim evidence.  The window lets that bulk go
without touching the text, the lexical index, or anything derived from it.
"""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from scope_recall.maintenance import doctor
from scope_recall.runtime import vector_retention
from scope_recall.runtime.instance import VectorRuntimeConfig
from test_v11_claims import app, capture

SPACE = "TEST-space"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.deleted: list[list[str]] = []

    def delete_by_ids(self, ids):
        self.deleted.append(list(ids))


class Config:
    """The two things a pass reads from a RuntimeInstanceConfig."""

    def __init__(self, vector):
        self.vector = vector

    def embedding_space_id(self):
        return SPACE


def _vector(tmp_path, days):
    return VectorRuntimeConfig(backend="sqlite-bruteforce", storage_dir=tmp_path / "vectors", table_name="TEST-vectors",
                               dimensions=1, test_injection_override=True, tool_output_retention_days=days)


def _embedded(core, *, aged, persisted_at="2026-01-01T12:00:00Z"):
    """Every source has finished embed work; the aged ones entered the store long ago."""
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("""INSERT OR IGNORE INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,state,available_at)
                        SELECT 'embed',event_id,source_revision,scope_id,project_id,branch_id,'done',persisted_at FROM source_events""")
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.executemany("UPDATE source_events SET persisted_at=? WHERE event_id=?", [(persisted_at, s.ref) for s in aged])
        conn.commit()


def _count(core, sql, *args):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(sql, args).fetchone()[0]


def _pass(core, ctx, store, tmp_path, *, days=180, now=NOW, seconds=30.0):
    return vector_retention.expire_if_due(store, Config(_vector(tmp_path, days)), core.storage, ctx,
                                          available_seconds=seconds, now=now)


def test_a_tool_output_older_than_the_window_loses_its_vector_and_nothing_else(app, tmp_path):
    core, ctx = app
    old_tools = [capture(core, ctx, f"TEST old tool output {index}", origin="tool_observation") for index in range(3)]
    capture(core, ctx, "TEST fresh tool output", origin="tool_observation")
    old_human = capture(core, ctx, "TEST old human turn")
    _embedded(core, aged=(*old_tools, old_human))
    store = Store()
    receipt = _pass(core, ctx, store, tmp_path)
    assert (receipt["outcome"], receipt["expired"], receipt["backlog"]) == ("expired", 3, False)
    assert store.deleted == [[f"p10:{s.ref}@1:{SPACE}" for s in old_tools]]
    assert _count(core, "SELECT count(*) FROM expired_vectors") == 3
    # The source, its lexical index and its finished work item are untouched...
    assert _count(core, "SELECT count(*) FROM source_events") == 5
    assert _count(core, "SELECT count(*) FROM lexical_projection WHERE event_id=?", old_tools[0].ref) > 0
    assert core.source(ctx, old_tools[0].ref, 1) is not None
    assert _count(core, "SELECT count(*) FROM work_items WHERE work_type='embed' AND state='done'") == 5
    # ...so nothing refills an embed for an expired source.
    core.resume_deferred(ctx, limit=16, remaining_seconds=1)
    assert _count(core, "SELECT count(*) FROM work_items WHERE work_type='embed'") == 5
    # Within the hour the drain leaves it alone; an hour later a pass finds nothing due.
    assert _pass(core, ctx, store, tmp_path, now=NOW + timedelta(minutes=30)) is None
    later = _pass(core, ctx, store, tmp_path, now=NOW + timedelta(hours=2))
    assert later["outcome"] == "nothing_due" and len(store.deleted) == 1


def test_a_backlog_is_cleared_one_batch_per_drain(app, tmp_path, monkeypatch):
    core, ctx = app
    monkeypatch.setattr(vector_retention, "BATCH_LIMIT", 2)
    tools = [capture(core, ctx, f"TEST backlog {index}", origin="tool_observation") for index in range(3)]
    _embedded(core, aged=tools)
    store = Store()
    first = _pass(core, ctx, store, tmp_path)
    assert (first["expired"], first["backlog"]) == (2, True)
    second = _pass(core, ctx, store, tmp_path)  # due at once: the last pass was full
    assert (second["expired"], second["backlog"]) == (1, False)
    assert [len(batch) for batch in store.deleted] == [2, 1]
    assert _count(core, "SELECT count(*) FROM expired_vectors") == 3


def test_no_window_no_store_or_no_budget_means_no_pass(app, tmp_path):
    core, ctx = app
    tool = capture(core, ctx, "TEST tool output", origin="tool_observation")
    _embedded(core, aged=[tool])
    store = Store()
    assert _pass(core, ctx, store, tmp_path, days=0) is None
    assert _pass(core, ctx, None, tmp_path) is None
    assert _pass(core, ctx, store, tmp_path, seconds=vector_retention.RESERVE_SECONDS - 0.1) is None
    assert store.deleted == [] and _count(core, "SELECT count(*) FROM expired_vectors") == 0


def test_a_failed_delete_records_nothing_and_is_retried(app, tmp_path):
    core, ctx = app
    tool = capture(core, ctx, "TEST tool output", origin="tool_observation")
    _embedded(core, aged=[tool])

    class Refusing:
        def delete_by_ids(self, ids):
            raise RuntimeError("TEST store down")

    receipt = _pass(core, ctx, Refusing(), tmp_path)
    assert (receipt["outcome"], receipt["error"]) == ("failed", "RuntimeError")
    assert _count(core, "SELECT count(*) FROM expired_vectors") == 0
    assert _pass(core, ctx, Store(), tmp_path, now=NOW + timedelta(hours=2))["expired"] == 1


def test_the_window_is_a_vector_setting_with_a_180_day_default(tmp_path):
    raw = {"backend": "sqlite-bruteforce", "storage_dir": str(tmp_path / "vectors"), "table_name": "TEST",
           "dimensions": 1, "test_injection_override": True}
    assert VectorRuntimeConfig.from_mapping(raw).tool_output_retention_days == 180
    assert VectorRuntimeConfig.from_mapping({**raw, "tool_output_retention_days": 0}).tool_output_retention_days == 0
    for bad in (-1, True, "180", 1.5, 36501):
        with pytest.raises(ValueError, match="tool_output_retention_days"):
            VectorRuntimeConfig.from_mapping({**raw, "tool_output_retention_days": bad})


def test_the_doctor_counts_expired_vectors_and_names_the_window(app, tmp_path):
    core, ctx = app
    tool = capture(core, ctx, "TEST tool output", origin="tool_observation")
    _embedded(core, aged=[tool])
    _pass(core, ctx, Store(), tmp_path)
    report = doctor.DoctorReport(host="hermes", status="degraded")
    doctor._check_index(report, core.storage.path.parent, store_readable=True, config=Config(_vector(tmp_path, 180)))
    assert report.index_metadata["expired_vectors"] == 1
    assert report.index_metadata["tool_output_retention_days"] == 180
