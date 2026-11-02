"""Handoff storage upgrades automatically and preserves a rollback snapshot."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import storage
from scope_recall.core.schema import SCHEMA_VERSION
from test_claims import app, initial
from test_handoff import app as handoff_app
from test_handoff import prepare, write
from v11_support import downgrade_store


def test_handoff_upgrade_preserves_sources_facts_and_snapshot(app, tmp_path):
    core, ctx = app
    claim, source = initial(core, ctx)
    before = core.current_claim(ctx, claim.ref).payload
    downgrade_store(core.storage.path, 1126)
    core.storage.initialize()
    core.storage.initialize()
    assert core.current_claim(ctx, claim.ref).payload == before
    assert core.source(ctx, source.ref, 1).event == source.event
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT count(*) FROM handoffs").fetchone()[0] == 0
    snapshots = tuple(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))
    assert len(snapshots) == 1
    with closing(sqlite3.connect(snapshots[0])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1126
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )

    restored = tmp_path / "TEST-restored.sqlite3"
    with closing(sqlite3.connect(snapshots[0])) as snapshot, closing(sqlite3.connect(restored)) as destination:
        snapshot.backup(destination)
        assert destination.execute("PRAGMA user_version").fetchone()[0] == 1126
        assert (
            destination.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )
        assert not destination.execute("SELECT 1 FROM sqlite_master WHERE name='handoffs'").fetchone()


def test_interrupted_handoff_upgrade_rolls_back(app, monkeypatch):
    core, ctx = app
    claim, _source = initial(core, ctx)
    downgrade_store(core.storage.path, 1126)
    step = storage.SCHEMA_MIGRATIONS[1126]

    def fail(conn):
        step(conn)
        raise RuntimeError("TEST handoff migration interrupted")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1126, fail)
    with pytest.raises(RuntimeError, match="TEST handoff"):
        core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1126
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='handoffs'").fetchone()
        assert conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (claim.ref,)).fetchone()


@pytest.mark.parametrize("interrupted", [False, True])
def test_session_provenance_upgrade_preserves_existing_report(handoff_app, monkeypatch, interrupted):
    core, sender, _source, payload = prepare(handoff_app)
    saved = write(core, sender, payload)
    with core.storage.read(sender) as tx:
        before = (
            tx._check().execute("SELECT resume_json FROM handoffs WHERE handoff_id=?", (saved["ref"],)).fetchone()[0]
        )
    downgrade_store(core.storage.path, 1127)
    if interrupted:
        step = storage.SCHEMA_MIGRATIONS[1127]

        def fail(conn):
            step(conn)
            raise RuntimeError("TEST interrupted provenance upgrade")

        monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1127, fail)
        with pytest.raises(RuntimeError, match="TEST interrupted provenance"):
            core.storage.initialize()
    else:
        core.storage.initialize()
        core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert (
            conn.execute("SELECT resume_json FROM handoffs WHERE handoff_id=?", (saved["ref"],)).fetchone()[0] == before
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(handoffs)")}
        assert ("sender_session" in columns) is not interrupted
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (1127 if interrupted else SCHEMA_VERSION)
        if not interrupted:
            assert (
                conn.execute("SELECT sender_session FROM handoffs WHERE handoff_id=?", (saved["ref"],)).fetchone()[0]
                is None
            )
