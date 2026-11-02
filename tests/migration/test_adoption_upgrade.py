"""Adoption schema migration preserves source truth and coordinated rollback."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import storage
from scope_recall.core.schema import SCHEMA_VERSION

from tests.contract.test_claims import app, capture
from tests.v11_support import downgrade_store


def test_adoption_schema_upgrade_preserves_existing_sources_and_snapshot(app):
    core, ctx = app
    source = capture(core, ctx, "TEST migration source remains unchanged.")
    downgrade_store(core.storage.path, 1118)
    core.storage.initialize()
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0
    assert core.source(ctx, source.ref, 1).event == source.event
    snapshots = tuple(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))
    assert len(snapshots) == 1
    with closing(sqlite3.connect(snapshots[0])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1118
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )


def test_failed_adoption_schema_step_rolls_back_without_publishing_new_format(app, monkeypatch):
    core, ctx = app
    source = capture(core, ctx, "TEST rollback preserves this source.")
    downgrade_store(core.storage.path, 1118)
    step = storage.SCHEMA_MIGRATIONS[1118]

    def fail(conn):
        step(conn)
        raise RuntimeError("TEST interrupted adoption migration")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1118, fail)
    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1118
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='adoption_relations'").fetchone() is None
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )
