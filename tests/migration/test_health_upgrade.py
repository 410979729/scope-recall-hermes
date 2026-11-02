"""Synthetic upgrade preserves facts while fencing old readers of new decisions."""

import sqlite3

from scope_recall.core import schema

from tests.contract.test_claims import app


def test_health_upgrade_stamps_the_next_schema_without_changing_facts():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE instance_meta(singleton INTEGER PRIMARY KEY, schema_version INTEGER)")
        conn.execute("INSERT INTO instance_meta VALUES(1,1125)")
        conn.execute("CREATE TABLE facts(value TEXT)")
        conn.execute("INSERT INTO facts VALUES('TEST-synthetic')")
        conn.execute("PRAGMA user_version=1125")
        schema.upgrade_1125(conn)
        assert schema.SCHEMA_VERSION == 1126
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1126
        assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == 1126
        assert conn.execute("SELECT value FROM facts").fetchone()[0] == "TEST-synthetic"


def test_health_real_upgrade_preserves_truth_and_snapshot(app):
    from contextlib import closing

    from tests.contract.test_claims import initial
    from tests.v11_support import downgrade_store

    core, ctx = app
    item, source = initial(core, ctx)
    before = core.current_claim(ctx, item.ref).payload
    downgrade_store(core.storage.path, 1125)
    core.storage.initialize()
    assert core.current_claim(ctx, item.ref).payload == before
    assert core.source(ctx, source.ref, source.revision).event == source.event
    snapshots = tuple(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))
    assert len(snapshots) == 1
    with closing(sqlite3.connect(snapshots[0])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1125


def test_health_real_upgrade_failure_rolls_back(app, monkeypatch):
    import pytest
    from scope_recall.core import storage

    from tests.contract.test_claims import capture
    from tests.v11_support import downgrade_store

    core, ctx = app
    source = capture(core, ctx, "TEST migration rollback source")
    downgrade_store(core.storage.path, 1125)
    step = storage.SCHEMA_MIGRATIONS[1125]

    def fail(conn):
        step(conn)
        raise RuntimeError("TEST interrupted health upgrade")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1125, fail)
    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.storage.initialize()
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1125
        assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == 1125
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )
