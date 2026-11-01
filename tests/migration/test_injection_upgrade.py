"""Source-screening tables use the coordinated, rollback-safe schema chain."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.schema import SCHEMA_VERSION
from v11_support import context, downgrade_store, source_event


@pytest.mark.parametrize("version", range(1108, 1113))
def test_supported_store_keeps_truth_and_seeds_rules_with_snapshot(tmp_path, version):
    ctx = context(tmp_path / "TEST-upgrade")
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    saved = core.record_event(ctx, source_event(), scope_id="TEST-scope", remaining_seconds=10).event_refs[0]
    digest = hashlib.sha256(core.source(ctx, saved.ref, 1).event["content"].encode()).hexdigest()
    downgrade_store(core.storage.path, version)
    core.storage.initialize(remaining_seconds=30)
    assert core.status(ctx).schema_version == SCHEMA_VERSION
    assert hashlib.sha256(core.source(ctx, saved.ref, 1).event["content"].encode()).hexdigest() == digest
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("SELECT count(*) FROM injection_rules").fetchone()[0] >= 10
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    state = json.loads((ctx.binding.data_directory / "store-upgrade.json").read_text())
    assert state["snapshot_verified"]
    with closing(sqlite3.connect(state["backup"])) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert (
            db.execute("SELECT content_sha256 FROM source_events WHERE event_id=?", (saved.ref,)).fetchone()[0]
            == digest
        )
    core.storage.initialize(remaining_seconds=30)
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1


def test_failed_screening_schema_step_rolls_back_without_touching_truth(tmp_path, monkeypatch):
    from scope_recall.core import storage

    ctx = context(tmp_path / "TEST-failed-upgrade")
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    core.record_event(ctx, source_event(), scope_id="TEST-scope", remaining_seconds=10)
    downgrade_store(core.storage.path, 1112)
    original = storage.SCHEMA_MIGRATIONS[1112]

    def fail(connection):
        original(connection)
        raise RuntimeError("TEST migration interruption")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1112, fail)
    with pytest.raises(RuntimeError, match="TEST migration interruption"):
        core.storage.initialize(remaining_seconds=30)
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1112
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='source_safety'").fetchall() == []
