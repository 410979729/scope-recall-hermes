"""Companion receipts and backfill diagnostics remain independent of index engines."""

import types
from datetime import datetime, timedelta, timezone

from scope_recall.runtime.vector_upkeep import index_if_due
from scope_recall.vector import compaction as vc
from scope_recall.vector.store import build_vector_store


def _raw_store(space):
    store = build_vector_store("sqlite-vec", storage_dir=space, table_name="vectors", dimensions=8)
    store.open()
    store.close()


def test_state_round_trips_and_replaces_cleanly(tmp_path):
    vc.write_state(tmp_path, {"outcome": "failed", "retry_after": 1})
    vc.write_state(tmp_path, {"outcome": "indexed", "retry_after": 0})
    assert vc.read_state(tmp_path)["outcome"] == "indexed"
    assert vc.read_state(tmp_path)["retry_after"] == 0
    assert list(tmp_path.glob("*.partial")) == []


def test_unreadable_or_unrelated_receipt_is_absent(tmp_path):
    assert vc.read_state(tmp_path) == {}
    (tmp_path / vc.STATE_FILENAME).write_text("{ truncated", encoding="utf-8")
    assert vc.read_state(tmp_path) == {}
    (tmp_path / vc.STATE_FILENAME).write_text('{"schema":"TEST unrelated"}', encoding="utf-8")
    assert vc.read_state(tmp_path) == {}


def test_missing_companion_has_no_footprint(tmp_path):
    assert vc.instance_vector_footprints(tmp_path) == []


def test_raw_footprint_counts_the_companion_file(tmp_path):
    space = tmp_path / "vectors" / "TEST-space"
    _raw_store(space)
    [report] = vc.instance_vector_footprints(tmp_path)
    assert report["bytes"] == (space / "vectors.sqlite3").stat().st_size
    assert report["dimensions"] == 8
    assert report["index_watermark"] == 0


def test_index_upkeep_without_a_capable_store_is_a_no_op(tmp_path):
    config = types.SimpleNamespace(storage_dir=tmp_path)
    assert index_if_due(None, config, available_seconds=10) is None
    assert index_if_due(types.SimpleNamespace(), config, available_seconds=10) is None
    assert index_if_due(types.SimpleNamespace(maintain_index=lambda **kw: {}), None, available_seconds=10) is None


def test_a_failed_import_embedding_backfill_is_reported_beside_the_store(tmp_path):
    """A failed embedding backfill remains visible beside the raw companion."""
    from scope_recall.runtime.vector_upkeep import backfill_if_due

    space = tmp_path / "vectors" / "TEST-space"
    _raw_store(space)

    class _Unreadable:
        def read(self, *_args, **_kwargs):
            raise RuntimeError("TEST store unavailable")

    context = types.SimpleNamespace(allowed_scope_ids=frozenset({"TEST-scope"}), project_id=None, branch_id=None)
    receipt = backfill_if_due(_Unreadable(), context, types.SimpleNamespace(storage_dir=space))
    assert receipt["outcome"] == "failed"
    [report] = vc.instance_vector_footprints(tmp_path)
    assert (report["embed_backfill_outcome"], report["embed_backfill_error"]) == ("failed", "RuntimeError")
    assert report["last_embed_backfill_at"] == receipt["checked_at"]


def test_a_backfill_no_worker_looks_at_any_more_is_not_the_store_s_outcome(tmp_path):
    """Each worker partition keeps its own state: one whose worker no longer runs (a retry lane, a workspace used
    once) would otherwise keep the store at its last outcome for good."""
    from datetime import datetime, timedelta, timezone

    space = tmp_path / "vectors" / "TEST-space"
    _raw_store(space)
    now = datetime.now(timezone.utc)
    for scopes, checked, outcome, total in (
        (["TEST-a"], now - timedelta(days=5), "failed", 3),
        (["TEST-b"], now - timedelta(hours=1), "finished", 5),
    ):
        vc.write_state(
            space,
            {
                "checked_at": checked.isoformat(),
                "outcome": outcome,
                "queued_total": total,
                "error": "RuntimeError" if outcome == "failed" else None,
            },
            filename=vc.embed_backfill_filename(scopes, None, None),
            schema=vc.EMBED_BACKFILL_STATE_SCHEMA,
        )
    # Unpartitioned receipts must not override a worker partition's outcome.
    vc.write_state(
        space,
        {"checked_at": now.isoformat(), "outcome": "failed"},
        filename="embed-backfill-state.json",
        schema=vc.EMBED_BACKFILL_STATE_SCHEMA,
    )
    [report] = vc.instance_vector_footprints(tmp_path)
    assert (report["embed_backfill_outcome"], report["embed_backfill_error"]) == ("finished", None)
    assert report["embed_backfill_queued_total"] == 8
