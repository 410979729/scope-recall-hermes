"""Review policy upgrades are automatic, snapshotted and transactionally reversible."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import storage
from scope_recall.core.schema import SCHEMA_VERSION

from tests.contract.test_claims import app, capture
from tests.v11_support import downgrade_store


@pytest.mark.parametrize("version", range(1122, 1131))
def test_review_upgrade_preserves_sources_and_default_policy(app, version):
    core, ctx = app
    source = capture(core, ctx, "TEST review migration source.")
    downgrade_store(core.storage.path, version)
    core.storage.initialize()
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert tx._check().execute("SELECT count(*) FROM scope_review_policy").fetchone()[0] == 0
    assert core.source(ctx, source.ref, 1).event == source.event
    snapshots = list(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))
    assert len(snapshots) == 1
    with closing(sqlite3.connect(snapshots[0])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )


@pytest.mark.parametrize("version", range(1122, 1131))
def test_review_upgrade_failure_restores_previous_schema_and_source(app, monkeypatch, version):
    core, ctx = app
    source = capture(core, ctx, "TEST review rollback source.")
    downgrade_store(core.storage.path, version)
    step = storage.SCHEMA_MIGRATIONS[1130]

    def fail(conn):
        step(conn)
        raise RuntimeError("TEST interrupted review upgrade")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1130, fail)
    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='scope_review_policy'").fetchone() is None
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )
    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1130, step)
    core.storage.initialize()
    assert core.source(ctx, source.ref, 1).event == source.event
