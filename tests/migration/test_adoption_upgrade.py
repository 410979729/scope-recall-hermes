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
    downgrade_store(core.storage.path, 1120)
    core.storage.initialize()
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0
    assert core.source(ctx, source.ref, 1).event == source.event
    snapshots = tuple(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))
    assert len(snapshots) == 1
    with closing(sqlite3.connect(snapshots[0])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1120
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )


def test_failed_adoption_schema_step_rolls_back_without_publishing_new_format(app, monkeypatch):
    core, ctx = app
    source = capture(core, ctx, "TEST rollback preserves this source.")
    downgrade_store(core.storage.path, 1120)
    step = storage.SCHEMA_MIGRATIONS[1120]

    def fail(conn):
        step(conn)
        raise RuntimeError("TEST interrupted adoption migration")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1120, fail)
    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1120
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='adoption_relations'").fetchone() is None
        assert (
            conn.execute("SELECT content FROM source_events WHERE event_id=?", (source.ref,)).fetchone()[0]
            == source.event["content"]
        )


@pytest.mark.parametrize("version", range(1105, 1122))
def test_merged_upgrade_chain_preserves_truth_and_existing_features(app, version):
    from scope_recall.maintenance.shared_import import SOURCE_SCHEMAS

    from tests.contract.test_claims import initial
    from tests.contract.test_importance_summaries import build

    core, ctx = app
    item, source = initial(core, ctx)
    if version >= 1120:
        with core.storage.write(ctx) as tx:
            tx._check().execute(
                "UPDATE claims SET importance=4,importance_assigned=1,importance_owner=1 WHERE claim_id=?", (item.ref,)
            )
        build(core, ctx)
    with closing(sqlite3.connect(core.storage.path)) as db:
        originals = db.execute(
            "SELECT event_id,content,content_sha256 FROM source_events ORDER BY source_id"
        ).fetchall()
        identity = db.execute("SELECT database_id FROM instance_meta").fetchone()[0]
        groups = db.execute("SELECT * FROM summary_groups").fetchall()
        parts = db.execute("SELECT * FROM summary_parts").fetchall()
    downgrade_store(core.storage.path, version)
    with closing(sqlite3.connect(core.storage.path)) as db:
        if version >= 1114:
            db.execute(
                "INSERT INTO injection_rules VALUES "
                "('TEST-merged-rule','instruction','regex','TEST protected','pending_review',0)"
            )
        db.commit()
    core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1123
        assert (
            db.execute("SELECT event_id,content,content_sha256 FROM source_events ORDER BY source_id").fetchall()
            == originals
        )
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables >= {
            "adoption_relations",
            "adoption_processing",
            "summary_groups",
            "summary_parts",
            "summary_backfill",
            "source_safety",
            "injection_rules",
            "claim_lexical_heads",
            "claim_lexical_postings",
        }
        assert db.execute("SELECT 1 FROM claim_lexical_postings WHERE claim_id=?", (item.ref,)).fetchone()
        if version >= 1119:
            assert db.execute("SELECT database_id FROM instance_meta").fetchone()[0] == identity
        if version >= 1120:
            assert db.execute(
                "SELECT importance,importance_owner FROM claims WHERE claim_id=?", (item.ref,)
            ).fetchone() == (4, 1)
            assert db.execute("SELECT * FROM summary_groups").fetchall() == groups
            assert db.execute("SELECT * FROM summary_parts").fetchall() == parts
        if version >= 1114:
            assert db.execute(
                "SELECT pattern,enabled FROM injection_rules WHERE rule_id='TEST-merged-rule'"
            ).fetchone() == ("TEST protected", 0)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert SOURCE_SCHEMAS == frozenset(range(1109, SCHEMA_VERSION + 1))
