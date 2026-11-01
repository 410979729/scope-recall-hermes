"""Raw embeddings survive index lag and readers without extension loading."""

import sqlite3
from datetime import UTC

import pytest
from scope_recall.vector.store import build_vector_store

from tests.contract.test_trace import app, edge  # noqa: F401 - shared truth fixture.


def row(key, vector, scope="scope"):
    return dict(id=key, scope_id=scope, source=key, target="{}", content="", summary="", updated_at="", vector=vector)


def test_raw_vectors_are_float32_in_separate_file(tmp_path):
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        store.upsert_records([row("one", [1.0] + [0.0] * 7)])
        with sqlite3.connect(tmp_path / "vectors.sqlite3") as conn:
            assert conn.execute("SELECT typeof(vector),length(vector) FROM vector_records").fetchone() == ("blob", 32)
    finally:
        store.close()


def test_reader_sees_worker_writes_after_index_watermark(tmp_path):
    writer = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    reader = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    writer.open()
    try:
        writer.upsert_records([row("old", [0.0, 1.0] + [0.0] * 6)])
        writer.maintain_index(max_rows=100)
        reader.open_existing()
        writer.upsert_records([row("new", [1.0] + [0.0] * 7)])
        assert reader.search([1.0] + [0.0] * 7, scope_id="scope", limit=1)[0]["id"] == "new"
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("legacy", ["lancedb", "sqlite-bruteforce"])
def test_old_backend_names_select_the_single_new_implementation(legacy, tmp_path):
    store = build_vector_store(legacy, storage_dir=tmp_path, table_name="vectors", dimensions=8)
    assert store.backend == "sqlite"


def test_extensionless_reader_and_writer_use_same_raw_file(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        store.upsert_records([row("one", [1.0] + [0.0] * 7), row("two", [0.0, 1.0] + [0.0] * 6)])
        assert store.maintain_index()["outcome"] == "indexed"
        assert store.diagnostics()["engine"] == "numpy-exact"
        assert store.search([1.0] + [0.0] * 7, scope_id="scope", limit=1)[0]["id"] == "one"
    finally:
        store.close()


def test_failed_group_rolls_back_all_rows(tmp_path):
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        with pytest.raises(ValueError):
            store.upsert_records([row("valid", [1.0] * 8), row("invalid", [float("nan")] * 8)])
        assert store.list_ids() == []
    finally:
        store.close()


def test_deleted_vector_stays_deleted_after_republication_and_index_lag(tmp_path):
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        payload = row("gone", [1.0] + [0.0] * 7)
        store.upsert_records([payload])
        store.maintain_index()
        store.delete_by_ids(["gone"])
        store.upsert_records([payload])
        assert store.list_ids() == []
        assert store.search([1.0] + [0.0] * 7, scope_id="scope", limit=1) == []
        store.maintain_index()
    finally:
        store.close()


def _legacy_json(path, records):
    import json

    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE vector_meta(key TEXT PRIMARY KEY,value TEXT)")
        conn.executemany("INSERT INTO vector_meta VALUES(?,?)", [("dimensions", "8"), ("table_name", "vectors")])
        conn.execute(
            "CREATE TABLE vector_records(id TEXT PRIMARY KEY,scope_id TEXT,source TEXT,target TEXT,"
            "content TEXT,summary TEXT,updated_at TEXT,vector_json TEXT)"
        )
        conn.executemany(
            "INSERT INTO vector_records VALUES(?,?,?,?,?,?,?,?)",
            [(r["id"], r["scope_id"], r["source"], r["target"], "", "", "", json.dumps(r["vector"])) for r in records],
        )


def test_json_import_resumes_catches_up_and_obeys_deletion_fence(tmp_path):
    from scope_recall.vector.imports import import_page

    _legacy_json(tmp_path / "vector.sqlite3", [row("one", [1.0] * 8), row("gone", [0.5] * 8)])
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        with pytest.raises(RuntimeError, match="vector_import_pending"):
            store.search([1.0] * 8, scope_id="scope", limit=1)
        store.delete_by_ids(["gone"])
        assert import_page(store, visible=lambda r: True, max_rows=1)["rows"] == 1
        store.close()
        store.open()
        store.upsert_records([row("new", [0.25] * 8)])
        for _ in range(12):
            outcome = import_page(store, visible=lambda r: True, max_rows=1)
            if outcome["outcome"] == "complete":
                break
        assert outcome["outcome"] == "complete"
        assert store.list_ids() == ["new", "one"]
        assert store.search([1.0] * 8, scope_id="scope", limit=2)
    finally:
        store.close()


def test_unreadable_import_is_explicit_and_keeps_input(tmp_path):
    from scope_recall.vector.imports import import_page

    old = tmp_path / "vector.sqlite3"
    old.write_bytes(b"TEST unreadable legacy companion")
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        assert import_page(store, visible=lambda r: True)["outcome"] == "unreadable"
        assert old.read_bytes() == b"TEST unreadable legacy companion"
        with pytest.raises(RuntimeError, match="vector_import_pending"):
            store.search([1.0] * 8, scope_id="scope", limit=1)
    finally:
        store.close()


def _finish_import(store):
    from scope_recall.vector.imports import import_page

    for _ in range(30):
        result = import_page(store, visible=lambda record: True, max_rows=1)
        if result["outcome"] == "complete":
            return result
    raise AssertionError(result)


def test_import_source_inventory_shrink_removes_only_import_owned_rows(tmp_path):
    from scope_recall.vector.imports import import_page

    _legacy_json(tmp_path / "vector.sqlite3", [row("one", [1.0] * 8), row("two", [0.5] * 8)])
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        import_page(store, visible=lambda record: True, max_rows=1)
        with sqlite3.connect(tmp_path / "vector.sqlite3") as old:
            old.execute("DELETE FROM vector_records WHERE id='one'")
        _finish_import(store)
        assert store.list_ids() == ["two"]
    finally:
        store.close()


def test_import_surplus_does_not_delete_new_publication_of_same_id(tmp_path):
    from scope_recall.vector.imports import import_page

    _legacy_json(tmp_path / "vector.sqlite3", [row("one", [1.0] * 8), row("two", [0.5] * 8)])
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        import_page(store, visible=lambda record: True, max_rows=1)
        store.upsert_records([row("one", [0.25] * 8)])
        with sqlite3.connect(tmp_path / "vector.sqlite3") as old:
            old.execute("DELETE FROM vector_records WHERE id='one'")
        _finish_import(store)
        assert store.list_records()["one"]["vector"] == [0.25] * 8
    finally:
        store.close()


def test_import_same_id_with_different_object_identity_never_activates(tmp_path):
    import json

    from scope_recall.vector.imports import import_page

    old = row("one", [1.0] * 8)
    old["target"] = json.dumps(dict(object_ref="old", object_kind="event", object_revision=1, embedding_space="space"))
    _legacy_json(tmp_path / "vector.sqlite3", [old])
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        replacement = {**old, "target": old["target"].replace('"old"', '"new"')}
        store.upsert_records([replacement])
        assert import_page(store, visible=lambda record: True)["outcome"] == "unreadable"
        assert store.list_records()["one"]["target"] == replacement["target"]
    finally:
        store.close()


def test_object_revision_and_space_are_the_raw_idempotency_key(tmp_path):
    import json

    from scope_recall.vector import VectorStoreCompatibilityError

    first = row("one", [1.0] * 8)
    first["target"] = json.dumps(
        dict(object_ref="event", object_kind="event", object_revision=1, embedding_space="space")
    )
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        store.upsert_records([first])
        store.upsert_records([first])
        with pytest.raises(VectorStoreCompatibilityError):
            store.upsert_records([{**first, "id": "two"}])
        assert store.list_ids() == ["one"]
    finally:
        store.close()


def test_exact_scan_and_mapped_cutoff_keep_newest_duplicate(tmp_path):
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        records = [row(f"TEST:{i:05d}", [1.0] * 8) for i in range(4200)]
        records[1]["updated_at"] = "2028-01-01T00:00:00Z"
        store.upsert_records(records)
        assert store.search([1.0] * 8, scope_id="scope", limit=1)[0]["id"] == records[1]["id"]
        store.maintain_index(max_rows=5000)
        assert store.search([1.0] * 8, scope_id="scope", limit=1)[0]["id"] == records[1]["id"]
    finally:
        store.close()


def test_failed_writable_transition_can_reopen_raw_reader(tmp_path, monkeypatch):
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    store.upsert_records([row("one", [1.0] * 8)])
    store.close()
    store.open_existing()
    original = store._ensure_schema
    monkeypatch.setattr(store, "_ensure_schema", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("TEST locked")))
    with pytest.raises(sqlite3.OperationalError):
        store.open()
    assert store.requires_reopen
    monkeypatch.setattr(store, "_ensure_schema", original)
    store.open_existing()
    try:
        assert store.search([1.0] * 8, scope_id="scope", limit=1)[0]["id"] == "one"
    finally:
        store.close()


def test_pending_vector_pages_keep_the_existing_supervisor_awake(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from scope_recall.runtime.vector_upkeep import next_vector_wake

    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        store.upsert_records([row("one", [1.0] * 8)])
        config = SimpleNamespace(vector=SimpleNamespace(storage_dir=tmp_path))
        now = datetime.now(UTC)
        assert next_vector_wake(config, now)[1] == "vector_index"
        store.maintain_index()
        assert next_vector_wake(config, now) is None
    finally:
        store.close()


def test_missing_import_never_creates_a_replacement_legacy_directory(tmp_path):
    from scope_recall.vector.imports import import_page

    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        store.note_missing_legacy()
        assert import_page(store, visible=lambda record: True)["outcome"] == "unreadable"
        assert store.diagnostics()["imports"][0]["last_error"] == "FileNotFoundError"
        assert not (tmp_path / "legacy-vectors").exists()
    finally:
        store.close()


def test_source_inventory_shrink_preserves_another_sources_matching_identity(tmp_path):
    from scope_recall.vector.imports import _identity, _verify_inventory

    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        record = row("shared", [1.0] * 8)
        store.upsert_records([record])
        conn = store._require_conn()
        seq = conn.execute("SELECT seq FROM vector_records").fetchone()[0]
        conn.execute("INSERT INTO vector_import_owned VALUES('first','shared',?)", (seq,))
        conn.execute("INSERT INTO vector_import_expected VALUES('second','shared',?)", (_identity(record),))
        _verify_inventory(store, "first")
        conn.commit()
        assert store.list_ids() == ["shared"]
    finally:
        store.close()


def test_locked_legacy_source_obeys_remaining_worker_budget(tmp_path):
    import time

    from scope_recall.core.deadline import RequestDeadline, using_request_deadline

    old_path = tmp_path / "vector.sqlite3"
    _legacy_json(old_path, [row("one", [1.0] * 8)])
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        with sqlite3.connect(old_path) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            began = time.monotonic()
            with using_request_deadline(RequestDeadline.from_budget(0.05)):
                receipt = store.import_legacy_page(visible=lambda record: True)
            assert time.monotonic() - began < 0.75
            assert receipt["outcome"] == "unreadable"
            assert receipt["error"] == "OperationalError"
            assert store.list_ids() == []
    finally:
        store.close()


def test_index_failures_keep_one_absolute_retry_time(tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from scope_recall.runtime.vector_upkeep import index_if_due, next_vector_wake

    now = datetime.now(UTC)
    config = SimpleNamespace(storage_dir=tmp_path)
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    store.upsert_records([row("one", [1.0] * 8)])
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise sqlite3.OperationalError("TEST index unavailable")

    try:
        failing = SimpleNamespace(maintain_index=fail)
        receipt = index_if_due(failing, config, available_seconds=5, now=now)
        later = now + timedelta(seconds=1)
        held = index_if_due(failing, config, available_seconds=5, now=later)
        assert held["outcome"] == "held" and len(calls) == 1
        runtime = SimpleNamespace(vector=config)
        first = next_vector_wake(runtime, now)
        second = next_vector_wake(runtime, later)
        assert first == second
        assert first[0].timestamp() == receipt["retry_after"]
    finally:
        store.close()


def test_unreadable_raw_file_uses_persisted_retry_instead_of_sliding_it(tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from scope_recall.runtime.vector_upkeep import next_vector_wake, record_vector_failure

    (tmp_path / "vectors.sqlite3").write_bytes(b"TEST unreadable raw companion")
    config = SimpleNamespace(storage_dir=tmp_path)
    runtime = SimpleNamespace(vector=config)
    now = datetime.now(UTC)
    receipt = record_vector_failure(config, "DatabaseError", now=now)
    first = next_vector_wake(runtime, now)
    second = next_vector_wake(runtime, now + timedelta(seconds=1))
    assert first == second
    assert first[0].timestamp() == receipt["retry_after"]


def test_explicit_reembedding_authorization_survives_companion_release_interruption(app, tmp_path):
    from scope_recall.runtime.vector_upkeep import release_requested_reembedding

    from tests.contract.test_embedding_respace import SPACE_B, _finish_embeds, _page, _start

    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    store = build_vector_store("sqlite-vec", storage_dir=tmp_path, table_name="vectors", dimensions=8)
    store.open()
    try:
        store.note_missing_legacy()
        _start(core, ctx)
        assert _page(core, ctx)["outcome"] == "complete"
        # Truth committed the page; a replacement worker must still recognize its authorization.
        store.close()
        store.open()
        assert release_requested_reembedding(core.storage, ctx, store, SPACE_B)
        with core.storage.read(ctx) as tx:
            old_authorization = tx.work.respace_authorization(SPACE_B)
        store.note_missing_legacy(old_authorization)
        assert not release_requested_reembedding(core.storage, ctx, store, SPACE_B)
        _finish_embeds(core)
        _start(core, ctx, action="restart")
        assert _page(core, ctx)["outcome"] == "complete"
        assert release_requested_reembedding(core.storage, ctx, store, SPACE_B)
    finally:
        store.close()
